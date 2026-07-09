from __future__ import annotations

import asyncio
import time
from builtins import ExceptionGroup
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.enphase_ev.evse_runtime import ChargeModeResolution
from custom_components.enphase_ev.refresh_plan import (
    FOLLOWUP_STAGE,
    FOLLOWUP_PLAN,
    HEATPUMP_FOLLOWUP_PLAN,
    SITE_ONLY_FOLLOWUP_PLAN,
    bind_refresh_stage,
    bind_refresh_plan,
    build_followup_plan,
    build_heatpump_followup_plan,
    build_post_session_followup_plan,
    build_site_only_followup_plan,
    post_session_followup_plan,
    warmup_plan,
)
from custom_components.enphase_ev.refresh_runner import RefreshRunner
from custom_components.enphase_ev.state_models import (
    RefreshHealthState,
    StateBackedAttribute,
    install_state_descriptors,
)


def test_coordinator_state_models_proxy_runtime_attributes(coordinator_factory) -> None:
    coord = coordinator_factory()

    coord._network_errors = 3  # noqa: SLF001
    coord._battery_profile = "cost_savings"  # noqa: SLF001
    coord._devices_inventory_cache_until = 42.0  # noqa: SLF001
    coord._hems_devices_last_success_utc = datetime.now(UTC)  # noqa: SLF001
    coord._charge_mode_cache = {"ABC": ("GREEN_CHARGING", 1.0)}  # noqa: SLF001

    assert coord._network_errors == 3  # noqa: SLF001
    assert coord.refresh_state._network_errors == 3
    assert "_network_errors" not in coord.__dict__

    assert coord._battery_profile == "cost_savings"  # noqa: SLF001
    assert coord.battery_state._battery_profile == "cost_savings"
    assert "_battery_profile" not in coord.__dict__

    assert coord._devices_inventory_cache_until == 42.0  # noqa: SLF001
    assert coord.inventory_state._devices_inventory_cache_until == 42.0

    assert (
        coord._hems_devices_last_success_utc
        == coord.heatpump_state._hems_devices_last_success_utc
    )  # noqa: SLF001
    assert (
        coord._charge_mode_cache == coord.evse_state._charge_mode_cache
    )  # noqa: SLF001
    assert coord.coerce_optional_int("7") == 7


def test_state_backed_attribute_falls_back_to_instance_dict() -> None:
    class _Owner:
        direct = StateBackedAttribute("refresh_state", "_network_errors")

        def __init__(self) -> None:
            self.__dict__["_network_errors"] = 7

    owner = _Owner()

    assert owner.direct == 7

    owner.direct = 11

    assert owner.direct == 11
    assert owner.__dict__["_network_errors"] == 11
    assert isinstance(_Owner.direct, StateBackedAttribute)


def test_state_backed_attribute_uses_runtime_state_when_available() -> None:
    class _Owner:
        direct = StateBackedAttribute("refresh_state", "_network_errors")

        def __init__(self) -> None:
            self.refresh_state = RefreshHealthState()

    owner = _Owner()

    owner.direct = 5

    assert owner.direct == 5
    assert owner.refresh_state._network_errors == 5
    assert "_network_errors" not in owner.__dict__


def test_state_backed_attribute_raises_for_missing_value() -> None:
    class _Owner:
        direct = StateBackedAttribute("refresh_state", "_network_errors")

    with pytest.raises(AttributeError, match="_network_errors"):
        _ = _Owner().direct


def test_install_state_descriptors_preserves_existing_attributes() -> None:
    class _Owner:
        _network_errors = "keep"
        existing = "keep"

    install_state_descriptors(_Owner)

    assert _Owner.existing == "keep"
    assert _Owner._network_errors == "keep"


def test_coordinator_payload_health_state_delegates_to_diagnostics(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    state = coord._payload_health_state("devices_inventory")  # noqa: SLF001

    assert state is coord.diagnostics.payload_health_state("devices_inventory")
    assert state["available"] is True


def test_coordinator_issue_context_delegates_to_diagnostics(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    metrics, placeholders = coord._issue_context()  # noqa: SLF001

    assert (metrics, placeholders) == coord.diagnostics.issue_context()


def test_coordinator_missing_battery_runtime_raises_attribute_error() -> None:
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)

    with pytest.raises(AttributeError, match="battery_runtime"):
        _ = coord.battery_runtime


@pytest.mark.asyncio
async def test_coordinator_public_runtime_commands_delegate(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    battery_runtime = MagicMock()
    battery_runtime.async_set_charge_from_grid = AsyncMock()
    battery_runtime.async_refresh_grid_control_check = AsyncMock()
    battery_runtime.async_refresh_storm_guard_profile = AsyncMock()
    battery_runtime.async_refresh_storm_alert = AsyncMock()
    battery_runtime.async_opt_out_all_storm_alerts = AsyncMock()
    battery_runtime.async_set_grid_mode = AsyncMock()
    battery_runtime.async_set_grid_connection = AsyncMock()
    battery_runtime.async_set_storm_guard_enabled = AsyncMock()
    battery_runtime.async_set_storm_evse_enabled = AsyncMock()
    battery_runtime.async_set_discharge_to_grid_schedule_limit = AsyncMock()
    battery_runtime.async_set_restrict_battery_discharge_schedule_enabled = AsyncMock()
    inventory_runtime = MagicMock()
    inventory_runtime.async_ensure_system_dashboard_diagnostics = AsyncMock()
    coord.battery_runtime = battery_runtime
    coord.inventory_runtime = inventory_runtime

    await coord.async_set_charge_from_grid(True)
    await coord.battery_runtime.async_refresh_grid_control_check(force=True)
    await coord.async_refresh_storm_guard_profile(force=True)
    await coord.async_refresh_storm_alert(force=False)
    await coord.async_opt_out_all_storm_alerts()
    await coord.async_set_grid_mode("import_only", "123456")
    await coord.async_set_grid_connection(False, otp="123456")
    await coord.async_set_storm_guard_enabled(True)
    await coord.async_set_storm_evse_enabled(False)
    await coord.async_set_discharge_to_grid_schedule_limit(25)
    await coord.async_set_restrict_battery_discharge_schedule_enabled(True)
    await coord.async_ensure_system_dashboard_diagnostics()

    battery_runtime.async_set_charge_from_grid.assert_awaited_once_with(True)
    battery_runtime.async_refresh_grid_control_check.assert_awaited_once_with(
        force=True
    )
    battery_runtime.async_refresh_storm_guard_profile.assert_awaited_once_with(
        force=True
    )
    battery_runtime.async_refresh_storm_alert.assert_awaited_once_with(force=False)
    battery_runtime.async_opt_out_all_storm_alerts.assert_awaited_once_with()
    battery_runtime.async_set_grid_mode.assert_awaited_once_with(
        "import_only",
        "123456",
    )
    battery_runtime.async_set_grid_connection.assert_awaited_once_with(
        False,
        otp="123456",
    )
    battery_runtime.async_set_storm_guard_enabled.assert_awaited_once_with(True)
    battery_runtime.async_set_storm_evse_enabled.assert_awaited_once_with(False)
    battery_runtime.async_set_discharge_to_grid_schedule_limit.assert_awaited_once_with(
        25
    )
    battery_runtime.async_set_restrict_battery_discharge_schedule_enabled.assert_awaited_once_with(
        True
    )
    inventory_runtime.async_ensure_system_dashboard_diagnostics.assert_awaited_once_with()


def test_coordinator_public_runtime_properties_delegate(coordinator_factory) -> None:
    coord = coordinator_factory()
    sample_utc = datetime.now(UTC)
    start_utc = datetime.now(UTC)
    coord.heatpump_runtime = SimpleNamespace(
        heatpump_runtime_state={"device_uid": "HP-1"},
        heatpump_runtime_state_last_error="runtime boom",
        heatpump_daily_consumption={"daily_energy_wh": 10.0},
        heatpump_daily_consumption_last_error="daily boom",
        heatpump_power_w=550.0,
        heatpump_power_sample_utc=sample_utc,
        heatpump_power_start_utc=start_utc,
        heatpump_power_device_uid="HP-1",
        heatpump_power_source="hems_energy_consumption_delta:HP-1",
        heatpump_power_last_error="power boom",
        heatpump_runtime_diagnostics=lambda: {
            "power_snapshot": {"outcome": "selected_sample"}
        },
    )
    coord.inventory_runtime = SimpleNamespace(
        inverter_diagnostics_payloads=lambda: {"summary_counts": {"total": 1}},
        system_dashboard_diagnostics=lambda: {"hierarchy_summary": {"total_nodes": 1}},
    )

    assert coord.heatpump_runtime_state == {"device_uid": "HP-1"}
    assert coord.heatpump_runtime_state_last_error == "runtime boom"
    assert coord.heatpump_daily_consumption == {"daily_energy_wh": 10.0}
    assert coord.heatpump_daily_consumption_last_error == "daily boom"
    assert coord.heatpump_power_w == 550.0
    assert coord.heatpump_power_sample_utc == sample_utc
    assert coord.heatpump_power_start_utc == start_utc
    assert coord.heatpump_power_device_uid == "HP-1"
    assert coord.heatpump_power_source == "hems_energy_consumption_delta:HP-1"
    assert coord.heatpump_power_last_error == "power boom"
    assert coord.heatpump_runtime_diagnostics()["power_snapshot"]["outcome"] == (
        "selected_sample"
    )
    assert coord.inverter_diagnostics_payloads()["summary_counts"]["total"] == 1
    assert coord.system_dashboard_diagnostics()["hierarchy_summary"]["total_nodes"] == 1


@pytest.mark.asyncio
async def test_coordinator_diagnostics_and_gateway_summary_delegation(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord.inventory_runtime = SimpleNamespace(
        _gateway_inventory_summary_marker=lambda: "marker",
        _build_gateway_inventory_summary=lambda: {"gateway": 1},
    )
    coord._battery_status_payload = {"status": "cached"}  # noqa: SLF001
    coord.battery_runtime.async_refresh_battery_status = AsyncMock()  # type: ignore[method-assign]  # type: ignore[method-assign]  # noqa: SLF001

    await coord.async_ensure_battery_status_diagnostics()
    coord.battery_runtime.async_refresh_battery_status.assert_not_awaited()  # noqa: SLF001

    coord._battery_status_payload = None  # noqa: SLF001
    await coord.async_ensure_battery_status_diagnostics()
    coord.battery_runtime.async_refresh_battery_status.assert_awaited_once_with(
        force=True
    )  # noqa: SLF001

    assert coord.gateway_inventory_summary() == {"gateway": 1}
    assert coord.gateway_inventory_summary() == {"gateway": 1}


class _RefreshOwner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.evse_timeseries = MagicMock()
        self.evse_timeseries.async_refresh.side_effect = (
            lambda *, day_local: self._record(f"evse_timeseries:{day_local}")
        )
        self.evse_timeseries.refresh_due = MagicMock(return_value=True)
        self.energy = MagicMock()
        self.energy._async_refresh_site_energy.side_effect = lambda: self._record(
            "site_energy"
        )
        self.energy.site_energy_refresh_due = MagicMock(return_value=True)
        self.evse_feature_flags_runtime = SimpleNamespace(
            refresh_due=lambda: True,
        )
        self.battery_runtime = SimpleNamespace(
            async_refresh_grid_control_check=self._async_refresh_grid_control_check,
            async_refresh_grid_outage_context=self._async_refresh_grid_outage_context,
            async_refresh_battery_status=self._async_refresh_battery_status,
            battery_site_settings_refresh_due=lambda: True,
            battery_backup_history_refresh_due=lambda: True,
            battery_settings_refresh_due=lambda: True,
            battery_schedules_refresh_due=lambda: True,
            storm_guard_refresh_due=lambda: True,
            storm_alert_refresh_due=lambda: True,
            grid_control_check_refresh_due=lambda: True,
            grid_outage_context_refresh_due=lambda: True,
            dry_contact_settings_refresh_due=lambda: True,
            battery_status_refresh_due=lambda: True,
            ac_battery_devices_refresh_due=lambda: True,
        )
        self.inventory_runtime = SimpleNamespace(
            _async_refresh_devices_inventory=self._async_refresh_devices_inventory,
            _async_refresh_hems_devices=self._async_refresh_hems_devices,
            devices_inventory_refresh_due=lambda: True,
            hems_devices_refresh_due=lambda: True,
            inverters_refresh_due=lambda: True,
        )
        self.current_power_runtime = SimpleNamespace(
            refresh_due=lambda: True,
        )
        self.tariff_runtime = SimpleNamespace(
            async_refresh=lambda **_kwargs: self._record("tariff"),
            refresh_due=lambda: True,
        )
        self.heatpump_runtime = SimpleNamespace(
            heatpump_runtime_state_refresh_due=lambda: True,
            heatpump_daily_consumption_refresh_due=lambda: True,
            heatpump_power_refresh_due=lambda: True,
            has_type=lambda key: key == "heatpump",
            client=SimpleNamespace(hems_site_supported=True),
        )
        self.refresh_runner = SimpleNamespace(
            async_refresh_site_energy_for_warmup=self.async_refresh_site_energy_for_warmup,
            async_refresh_evse_timeseries_for_warmup=self.async_refresh_evse_timeseries_for_warmup,
            async_refresh_session_state_for_warmup=self.async_refresh_session_state_for_warmup,
            async_refresh_secondary_evse_state_for_warmup=self.async_refresh_secondary_evse_state_for_warmup,
        )

    def _record(self, value: str) -> str:
        self.calls.append(value)
        return value

    def _async_refresh_battery_site_settings(self) -> str:
        self.calls.append("battery_site_settings")
        return "site-settings"

    def _async_refresh_battery_backup_history(self) -> str:
        self.calls.append("battery_backup_history")
        return "backup-history"

    def _async_refresh_battery_settings(self) -> str:
        self.calls.append("battery_settings")
        return "settings"

    def _async_refresh_battery_schedules(self) -> str:
        self.calls.append("battery_schedules")
        return "schedules"

    def _async_refresh_storm_guard_profile(self) -> str:
        self.calls.append("storm_guard")
        return "storm-guard"

    def _async_refresh_storm_alert(self) -> str:
        self.calls.append("storm_alert")
        return "storm-alert"

    def _async_refresh_grid_control_check(self) -> str:
        self.calls.append("grid_control")
        return "grid-control"

    def _async_refresh_grid_outage_context(self) -> str:
        self.calls.append("grid_outage_context")
        return "grid-outage-context"

    def _async_refresh_dry_contact_settings(self) -> str:
        self.calls.append("dry_contact")
        return "dry-contact"

    def _async_refresh_current_power_consumption(self) -> str:
        self.calls.append("current_power")
        return "current-power"

    def _async_refresh_evse_feature_flags(self) -> str:
        self.calls.append("evse_feature_flags")
        return "evse-feature-flags"

    def _async_refresh_battery_status(self) -> str:
        self.calls.append("battery_status")
        return "battery-status"

    def _async_refresh_devices_inventory(self) -> str:
        self.calls.append("devices_inventory")
        return "devices-inventory"

    def _async_refresh_hems_devices(self) -> str:
        self.calls.append("hems_devices")
        return "hems-devices"

    def _async_refresh_inverters(self) -> str:
        self.calls.append("inverters")
        return "inverters"

    def _async_refresh_heatpump_runtime_state(self) -> str:
        self.calls.append("heatpump_runtime")
        return "heatpump-runtime"

    def _async_refresh_heatpump_daily_consumption(self) -> str:
        self.calls.append("heatpump_daily")
        return "heatpump-daily"

    def _async_refresh_heatpump_power(self) -> str:
        self.calls.append("heatpump_power")
        return "heatpump-power"

    def async_refresh_site_energy_for_warmup(self) -> str:
        self.calls.append("warmup_site_energy")
        return "warmup-site-energy"

    def async_refresh_evse_timeseries_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]],
    ) -> str:
        self.calls.append(f"warmup_evse_timeseries:{sorted(working_data)}")
        return "warmup-evse-timeseries"

    def async_refresh_session_state_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]],
    ) -> str:
        self.calls.append(f"warmup_sessions:{sorted(working_data)}")
        return "warmup-sessions"

    def async_refresh_secondary_evse_state_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]],
    ) -> str:
        self.calls.append(f"warmup_secondary:{sorted(working_data)}")
        return "warmup-secondary"


def test_followup_refresh_stage_binds_zero_arg_calls() -> None:
    owner = _RefreshOwner()
    bound = bind_refresh_stage(owner, FOLLOWUP_STAGE)

    assert bound.defer_topology is True
    assert [call[0] for call in bound.parallel_calls] == [
        "battery_site_settings_s",
        "battery_backup_history_s",
        "battery_settings_s",
        "battery_schedules_s",
        "storm_guard_s",
        "storm_alert_s",
        "tariff_s",
        "grid_control_check_s",
        "grid_outage_context_s",
        "dry_contact_settings_s",
        "current_power_s",
    ]
    assert [call[0] for call in bound.ordered_calls] == [
        "battery_status_s",
        "ac_battery_devices_s",
        "devices_inventory_s",
        "hems_devices_s",
    ]

    assert bound.parallel_calls[0][2]() == "site-settings"
    assert bound.ordered_calls[-1][2]() == "hems-devices"
    assert bound.parallel_calls[0][3] == "battery_site_settings"
    assert bound.ordered_calls[-1][3] == "inventory_topology"
    assert owner.calls == ["battery_site_settings", "hems_devices"]


def test_refresh_plans_bind_dynamic_followup_and_warmup_calls() -> None:
    owner = _RefreshOwner()
    working_data = {"SERIAL-1": {"ok": True}}

    bound_warmup = bind_refresh_plan(owner, warmup_plan(working_data))

    assert [stage.stage_key for stage in bound_warmup.stages] == [
        "discovery",
        "state",
        None,
        "energy",
    ]
    assert "tariff_s" in [call[0] for call in bound_warmup.stages[1].parallel_calls]
    assert bound_warmup.stages[2].ordered_calls[0][2]() == "heatpump-runtime"
    assert bound_warmup.stages[3].parallel_calls[0][2]() == "warmup-site-energy"
    assert bound_warmup.stages[3].parallel_calls[1][2]() == "warmup-evse-timeseries"
    assert bound_warmup.stages[3].parallel_calls[2][2]() == "warmup-sessions"
    assert bound_warmup.stages[3].parallel_calls[3][2]() == "warmup-secondary"

    bound_post = bind_refresh_plan(owner, post_session_followup_plan("today"))

    assert [stage.stage_key for stage in bound_post.stages] == [None]
    assert bound_post.stages[0].defer_topology is True
    assert bound_post.stages[0].parallel_calls[0][2]() == "evse_timeseries:today"
    assert bound_post.stages[0].parallel_calls[1][2]() == "site_energy"
    assert bound_post.stages[0].parallel_calls[2][2]() == "inverters"


def test_dynamic_followup_plan_skips_up_to_date_tasks() -> None:
    owner = _RefreshOwner()
    owner.battery_runtime.battery_site_settings_refresh_due = lambda: False
    owner.battery_runtime.battery_backup_history_refresh_due = lambda: False
    owner.battery_runtime.battery_settings_refresh_due = lambda: False
    owner.battery_runtime.battery_schedules_refresh_due = lambda: False
    owner.battery_runtime.storm_guard_refresh_due = lambda: False
    owner.battery_runtime.storm_alert_refresh_due = lambda: False
    owner.battery_runtime.grid_control_check_refresh_due = lambda: False
    owner.battery_runtime.grid_outage_context_refresh_due = lambda: False
    owner.battery_runtime.dry_contact_settings_refresh_due = lambda: False
    owner.battery_runtime.battery_status_refresh_due = lambda: False
    owner.battery_runtime.ac_battery_devices_refresh_due = lambda: False
    owner.inventory_runtime.devices_inventory_refresh_due = lambda: False
    owner.inventory_runtime.hems_devices_refresh_due = lambda: False
    owner.current_power_runtime.refresh_due = lambda: False
    owner.evse_feature_flags_runtime.refresh_due = lambda: False
    owner.tariff_runtime.refresh_due = lambda: False

    assert build_followup_plan(owner).stages == ()


def test_dynamic_followup_plan_selects_due_subset() -> None:
    owner = _RefreshOwner()
    owner.battery_runtime.battery_backup_history_refresh_due = lambda: False
    owner.battery_runtime.battery_settings_refresh_due = lambda: False
    owner.battery_runtime.battery_schedules_refresh_due = lambda: False
    owner.battery_runtime.storm_guard_refresh_due = lambda: False
    owner.battery_runtime.storm_alert_refresh_due = lambda: False
    owner.battery_runtime.grid_control_check_refresh_due = lambda: False
    owner.battery_runtime.dry_contact_settings_refresh_due = lambda: False
    owner.battery_runtime.ac_battery_devices_refresh_due = lambda: False
    owner.inventory_runtime.devices_inventory_refresh_due = lambda: False
    owner.current_power_runtime.refresh_due = lambda: False
    owner.evse_feature_flags_runtime.refresh_due = lambda: False

    plan = build_followup_plan(owner)

    assert len(plan.stages) == 1
    stage = plan.stages[0]
    assert [task.timing_key for task in stage.parallel_tasks] == [
        "battery_site_settings_s",
        "tariff_s",
        "grid_outage_context_s",
    ]
    assert [task.timing_key for task in stage.ordered_tasks] == [
        "battery_status_s",
        "hems_devices_s",
    ]


def test_dynamic_site_only_followup_plan_keeps_due_inverters() -> None:
    owner = _RefreshOwner()
    owner.inventory_runtime.inverters_refresh_due = lambda: True
    owner.heatpump_runtime.heatpump_power_refresh_due = lambda: False
    owner.heatpump_runtime.heatpump_runtime_state_refresh_due = lambda: False
    owner.heatpump_runtime.heatpump_daily_consumption_refresh_due = lambda: False

    plan = build_site_only_followup_plan(owner)

    assert len(plan.stages) == 1
    assert [task.timing_key for task in plan.stages[0].ordered_tasks][
        -1
    ] == "inverters_s"


def test_dynamic_site_only_followup_plan_creates_inverter_stage_without_base_followup() -> (
    None
):
    owner = _RefreshOwner()
    owner.battery_runtime.battery_site_settings_refresh_due = lambda: False
    owner.battery_runtime.battery_backup_history_refresh_due = lambda: False
    owner.battery_runtime.battery_settings_refresh_due = lambda: False
    owner.battery_runtime.battery_schedules_refresh_due = lambda: False
    owner.battery_runtime.storm_guard_refresh_due = lambda: False
    owner.battery_runtime.storm_alert_refresh_due = lambda: False
    owner.battery_runtime.grid_control_check_refresh_due = lambda: False
    owner.battery_runtime.grid_outage_context_refresh_due = lambda: False
    owner.battery_runtime.dry_contact_settings_refresh_due = lambda: False
    owner.battery_runtime.battery_status_refresh_due = lambda: False
    owner.battery_runtime.ac_battery_devices_refresh_due = lambda: False
    owner.inventory_runtime.devices_inventory_refresh_due = lambda: False
    owner.inventory_runtime.hems_devices_refresh_due = lambda: False
    owner.current_power_runtime.refresh_due = lambda: False
    owner.evse_feature_flags_runtime.refresh_due = lambda: False
    owner.tariff_runtime.refresh_due = lambda: False
    owner.inventory_runtime.inverters_refresh_due = lambda: True
    owner.heatpump_runtime.heatpump_runtime_state_refresh_due = lambda: False
    owner.heatpump_runtime.heatpump_daily_consumption_refresh_due = lambda: False
    owner.heatpump_runtime.heatpump_power_refresh_due = lambda: False

    plan = build_site_only_followup_plan(owner)

    assert len(plan.stages) == 1
    assert [task.timing_key for task in plan.stages[0].ordered_tasks] == [
        "inverters_s",
    ]


def test_dynamic_followup_plan_includes_due_evse_feature_flags() -> None:
    owner = _RefreshOwner()
    owner.battery_runtime.battery_site_settings_refresh_due = lambda: False
    owner.battery_runtime.battery_backup_history_refresh_due = lambda: False
    owner.battery_runtime.battery_settings_refresh_due = lambda: False
    owner.battery_runtime.battery_schedules_refresh_due = lambda: False
    owner.battery_runtime.storm_guard_refresh_due = lambda: False
    owner.battery_runtime.storm_alert_refresh_due = lambda: False
    owner.battery_runtime.grid_control_check_refresh_due = lambda: False
    owner.battery_runtime.grid_outage_context_refresh_due = lambda: False
    owner.battery_runtime.dry_contact_settings_refresh_due = lambda: False
    owner.battery_runtime.battery_status_refresh_due = lambda: False
    owner.battery_runtime.ac_battery_devices_refresh_due = lambda: False
    owner.inventory_runtime.devices_inventory_refresh_due = lambda: False
    owner.inventory_runtime.hems_devices_refresh_due = lambda: False
    owner.current_power_runtime.refresh_due = lambda: False
    owner.tariff_runtime.refresh_due = lambda: False

    plan = build_followup_plan(owner)

    assert len(plan.stages) == 1
    assert [task.timing_key for task in plan.stages[0].parallel_tasks] == [
        "evse_feature_flags_s",
    ]


def test_dynamic_plan_builders_force_full_plans() -> None:
    owner = _RefreshOwner()

    assert build_followup_plan(owner, force_full=True) == FOLLOWUP_PLAN
    assert (
        build_site_only_followup_plan(owner, force_full=True) == SITE_ONLY_FOLLOWUP_PLAN
    )
    assert (
        build_heatpump_followup_plan(owner, force_full=True) == HEATPUMP_FOLLOWUP_PLAN
    )
    built = build_post_session_followup_plan(owner, "today", force_full=True)
    static = post_session_followup_plan("today")
    assert len(built.stages) == len(static.stages) == 1
    assert built.stages[0].defer_topology is static.stages[0].defer_topology is True
    assert [task.timing_key for task in built.stages[0].parallel_tasks] == [
        task.timing_key for task in static.stages[0].parallel_tasks
    ]


def test_dynamic_heatpump_followup_prefers_power() -> None:
    owner = _RefreshOwner()

    plan = build_heatpump_followup_plan(owner)

    assert len(plan.stages) == 1
    assert [task.timing_key for task in plan.stages[0].ordered_tasks] == [
        "heatpump_power_s",
    ]


def test_dynamic_heatpump_followup_uses_runtime_and_daily_when_power_not_due() -> None:
    owner = _RefreshOwner()
    owner.heatpump_runtime.heatpump_power_refresh_due = lambda: False

    plan = build_heatpump_followup_plan(owner)

    assert len(plan.stages) == 1
    assert [task.timing_key for task in plan.stages[0].ordered_tasks] == [
        "heatpump_runtime_s",
        "heatpump_daily_s",
    ]


def test_dynamic_heatpump_followup_includes_cleanup_when_power_cannot_cover_deps() -> (
    None
):
    owner = _RefreshOwner()
    owner.heatpump_runtime.has_type = lambda _key: False

    plan = build_heatpump_followup_plan(owner)

    assert len(plan.stages) == 1
    assert [task.timing_key for task in plan.stages[0].ordered_tasks] == [
        "heatpump_runtime_s",
        "heatpump_daily_s",
        "heatpump_power_s",
    ]


def test_dynamic_heatpump_followup_includes_cleanup_when_hems_support_unknown() -> None:
    owner = _RefreshOwner()
    owner.heatpump_runtime.client.hems_site_supported = None

    plan = build_heatpump_followup_plan(owner)

    assert len(plan.stages) == 1
    assert [task.timing_key for task in plan.stages[0].ordered_tasks] == [
        "heatpump_runtime_s",
        "heatpump_daily_s",
        "heatpump_power_s",
    ]


def test_dynamic_post_session_followup_only_includes_due_tasks() -> None:
    owner = _RefreshOwner()
    owner.energy.site_energy_refresh_due = MagicMock(return_value=False)
    owner.inventory_runtime.inverters_refresh_due = lambda: False

    plan = build_post_session_followup_plan(owner, "today")

    assert len(plan.stages) == 1
    assert [task.timing_key for task in plan.stages[0].parallel_tasks] == [
        "evse_timeseries_s",
    ]


def test_dynamic_post_session_followup_includes_all_due_tasks() -> None:
    owner = _RefreshOwner()

    plan = build_post_session_followup_plan(owner, "today")

    assert len(plan.stages) == 1
    assert [task.timing_key for task in plan.stages[0].parallel_tasks] == [
        "evse_timeseries_s",
        "site_energy_s",
        "inverters_s",
    ]


@pytest.mark.asyncio
async def test_coordinator_refresh_plan_runner_executes_each_stage(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    seen: list[tuple[str | None, bool, int, int]] = []

    async def _run_stage(
        phase_timings: dict[str, float],
        *,
        parallel_calls,
        ordered_calls,
        stage_key=None,
        defer_topology=False,
    ) -> None:
        seen.append(
            (
                stage_key,
                defer_topology,
                len(parallel_calls),
                len(ordered_calls),
            )
        )

    coord.refresh_runner.async_run_staged_refresh_calls = _run_stage  # type: ignore[method-assign]

    await coord.refresh_runner.async_run_refresh_plan({}, plan=FOLLOWUP_PLAN)

    assert seen == [
        (None, True, 11, 4),
    ]


@pytest.mark.asyncio
async def test_coordinator_run_refresh_calls_tracks_stage_and_topology_batch(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._begin_topology_refresh_batch = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._end_topology_refresh_batch = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord.refresh_runner.async_run_refresh_call = AsyncMock(  # type: ignore[method-assign]
        side_effect=(("first_s", 0.1), ("second_s", 0.2))
    )
    phase_timings: dict[str, float] = {}

    await coord.refresh_runner.async_run_refresh_calls(
        phase_timings,
        calls=(
            ("first_s", "first", lambda: None, None),
            ("second_s", "second", lambda: None, None),
        ),
        stage_key="refresh",
        defer_topology=True,
    )

    coord._begin_topology_refresh_batch.assert_called_once_with()  # noqa: SLF001
    coord._end_topology_refresh_batch.assert_called_once_with()  # noqa: SLF001
    assert phase_timings["first_s"] == 0.1
    assert phase_timings["second_s"] == 0.2
    assert "refresh_s" in phase_timings


@pytest.mark.asyncio
async def test_coordinator_run_refresh_calls_drains_siblings_before_batch_end(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    started = asyncio.Event()
    drained = False
    end_saw_drained = False

    async def _slow() -> None:
        nonlocal drained
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            drained = True
            raise

    async def _fail() -> None:
        await started.wait()
        raise RuntimeError("boom")

    def _end_topology_batch() -> None:
        nonlocal end_saw_drained
        end_saw_drained = drained

    coord._begin_topology_refresh_batch = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._end_topology_refresh_batch = MagicMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=_end_topology_batch
    )

    with pytest.raises(RuntimeError, match="boom"):
        await coord.refresh_runner.async_run_refresh_calls(
            {},
            calls=(
                ("slow_s", "slow", _slow, None),
                ("fail_s", "fail", _fail, None),
            ),
            defer_topology=True,
        )

    coord._begin_topology_refresh_batch.assert_called_once_with()  # noqa: SLF001
    coord._end_topology_refresh_batch.assert_called_once_with()  # noqa: SLF001
    assert drained is True
    assert end_saw_drained is True


@pytest.mark.asyncio
async def test_coordinator_run_refresh_calls_cancelled_child_drains_siblings(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    started = asyncio.Event()
    drained = False
    end_saw_drained = False

    async def _slow() -> None:
        nonlocal drained
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            drained = True
            raise

    async def _cancel() -> None:
        await started.wait()
        raise asyncio.CancelledError

    def _end_topology_batch() -> None:
        nonlocal end_saw_drained
        end_saw_drained = drained

    coord._begin_topology_refresh_batch = MagicMock()  # type: ignore[method-assign]  # noqa: SLF001
    coord._end_topology_refresh_batch = MagicMock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=_end_topology_batch
    )

    with pytest.raises(asyncio.CancelledError):
        await coord.refresh_runner.async_run_refresh_calls(
            {},
            calls=(
                ("slow_s", "slow", _slow, None),
                ("cancel_s", "cancel", _cancel, None),
            ),
            defer_topology=True,
        )

    coord._begin_topology_refresh_batch.assert_called_once_with()  # noqa: SLF001
    coord._end_topology_refresh_batch.assert_called_once_with()  # noqa: SLF001
    assert drained is True
    assert end_saw_drained is True


@pytest.mark.asyncio
async def test_coordinator_run_refresh_calls_prefers_failure_over_child_cancel(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    started = 0
    ready = asyncio.Event()

    async def _wait_until_ready() -> None:
        nonlocal started
        started += 1
        if started == 2:
            ready.set()
        await ready.wait()

    async def _cancel() -> None:
        await _wait_until_ready()
        raise asyncio.CancelledError

    async def _fail() -> None:
        await _wait_until_ready()
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await coord.refresh_runner.async_run_refresh_calls(
            {},
            calls=(
                ("cancel_s", "cancel", _cancel, None),
                ("fail_s", "fail", _fail, None),
            ),
        )


@pytest.mark.asyncio
async def test_coordinator_run_refresh_calls_filters_child_cancel_from_failures(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    started = 0
    ready = asyncio.Event()

    async def _wait_until_ready() -> None:
        nonlocal started
        started += 1
        if started == 3:
            ready.set()
        await ready.wait()

    async def _cancel() -> None:
        await _wait_until_ready()
        raise asyncio.CancelledError

    async def _fail_runtime() -> None:
        await _wait_until_ready()
        raise RuntimeError("boom")

    async def _fail_value() -> None:
        await _wait_until_ready()
        raise ValueError("bad")

    with pytest.raises(ExceptionGroup) as exc_info:
        await coord.refresh_runner.async_run_refresh_calls(
            {},
            calls=(
                ("cancel_s", "cancel", _cancel, None),
                ("runtime_s", "runtime", _fail_runtime, None),
                ("value_s", "value", _fail_value, None),
            ),
        )

    assert {type(err) for err in exc_info.value.exceptions} == {
        RuntimeError,
        ValueError,
    }


@pytest.mark.asyncio
async def test_coordinator_run_refresh_calls_preserves_multiple_failures(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    async def _fail_runtime() -> None:
        raise RuntimeError("boom")

    async def _fail_value() -> None:
        raise ValueError("bad")

    with pytest.raises(ExceptionGroup) as exc_info:
        await coord.refresh_runner.async_run_refresh_calls(
            {},
            calls=(
                ("runtime_s", "runtime", _fail_runtime, None),
                ("value_s", "value", _fail_value, None),
            ),
        )

    assert {type(err) for err in exc_info.value.exceptions} == {
        RuntimeError,
        ValueError,
    }


@pytest.mark.asyncio
async def test_refresh_runner_staged_calls_track_empty_stage_timing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    runner = RefreshRunner(coord)
    phase_timings: dict[str, float] = {}

    await runner.async_run_staged_refresh_calls(phase_timings, stage_key="empty")

    assert phase_timings == {"empty_s": 0.0}


def test_coordinator_lazily_creates_refresh_runner() -> None:
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)

    runner = coord.refresh_runner

    assert isinstance(runner, RefreshRunner)
    assert runner is coord.refresh_runner


@pytest.mark.asyncio
async def test_refresh_runner_login_wall_raises_update_failed_with_retry_after(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.api import EnphaseLoginWallUnauthorized
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = coordinator_factory()
    coord._activate_auth_block_from_login_wall = MagicMock(return_value=True)  # type: ignore[method-assign]  # noqa: SLF001
    coord._blocked_auth_failure_message = MagicMock(return_value="blocked")  # type: ignore[method-assign]  # noqa: SLF001
    coord._auth_block_retry_after_s = MagicMock(return_value=42)  # type: ignore[method-assign]  # noqa: SLF001

    async def _raise() -> None:
        raise EnphaseLoginWallUnauthorized(
            endpoint="/service/test",
            request_label="GET /service/test",
            status=200,
            content_type="text/html; charset=utf-8",
            body_preview_redacted="<!DOCTYPE html>",
        )

    with pytest.raises(UpdateFailed, match="blocked") as exc_info:
        await coord.refresh_runner.async_run_refresh_call("k", "label", _raise)

    assert exc_info.value.retry_after == 42


@pytest.mark.asyncio
async def test_post_status_evse_enrichments_run_concurrently(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    phase_timings: dict[str, float] = {}
    release = asyncio.Event()
    started: list[str] = []
    all_started = asyncio.Event()

    async def _gate(name: str, result):
        started.append(name)
        if len(started) == 4:
            all_started.set()
        await release.wait()
        return result

    async def _charge_modes(_serials):
        return await _gate("charge_modes", {"SERIAL-1": "SMART_CHARGING"})

    async def _green_settings(_serials):
        return await _gate("green_settings", {"SERIAL-1": (True, True)})

    async def _auth_settings(_serials):
        return await _gate("auth_settings", {"SERIAL-1": (True, False, True, True)})

    async def _charger_config(_serials, *, keys):
        assert keys
        return await _gate("charger_config", {"SERIAL-1": {"foo": "bar"}})

    coord.evse_runtime.async_resolve_charge_modes = AsyncMock(side_effect=_charge_modes)
    coord._async_resolve_green_battery_settings = AsyncMock(  # noqa: SLF001
        side_effect=_green_settings
    )
    coord._async_resolve_auth_settings = AsyncMock(  # noqa: SLF001
        side_effect=_auth_settings
    )
    coord._async_resolve_charger_config = AsyncMock(  # noqa: SLF001
        side_effect=_charger_config
    )

    task = asyncio.create_task(
        coord._async_resolve_post_status_evse_enrichments(
            phase_timings,
            records=[("SERIAL-1", {"sn": "SERIAL-1"})],
            charge_mode_candidates=["SERIAL-1"],
            first_refresh=False,
        )
    )

    await asyncio.wait_for(all_started.wait(), timeout=1)
    assert started == [
        "charge_modes",
        "green_settings",
        "auth_settings",
        "charger_config",
    ]
    release.set()
    result = await asyncio.wait_for(task, timeout=1)

    assert result == (
        {"SERIAL-1": "SMART_CHARGING"},
        {"SERIAL-1": (True, True)},
        {"SERIAL-1": (True, False, True, True)},
        {"SERIAL-1": {"foo": "bar"}},
    )
    assert "charge_mode_s" in phase_timings
    assert "green_settings_s" in phase_timings
    assert "auth_settings_s" in phase_timings
    assert "charger_config_s" in phase_timings


@pytest.mark.asyncio
async def test_post_status_evse_enrichments_reuse_fresh_cache_without_lookup_tasks(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    phase_timings: dict[str, float] = {}
    now = time.monotonic()
    coord._charge_mode_cache = {"SERIAL-1": ("SMART_CHARGING", now)}  # noqa: SLF001
    coord._green_battery_cache = {"SERIAL-1": (True, True, now)}  # noqa: SLF001
    coord._auth_settings_cache = {  # noqa: SLF001
        "SERIAL-1": (True, False, True, True, now)
    }
    coord._charger_config_cache = {  # noqa: SLF001
        "SERIAL-1": ({"DefaultChargeLevel": 32, "phase_switch_config": "auto"}, now)
    }
    coord.evse_runtime.async_resolve_charge_modes = AsyncMock()
    coord._async_resolve_green_battery_settings = AsyncMock()  # noqa: SLF001
    coord._async_resolve_auth_settings = AsyncMock()  # noqa: SLF001
    coord._async_resolve_charger_config = AsyncMock()  # noqa: SLF001

    result = await coord._async_resolve_post_status_evse_enrichments(
        phase_timings,
        records=[("SERIAL-1", {"sn": "SERIAL-1"})],
        charge_mode_candidates=["SERIAL-1"],
        first_refresh=False,
    )

    assert result == (
        {"SERIAL-1": ChargeModeResolution("SMART_CHARGING", "cache")},
        {"SERIAL-1": (True, True)},
        {"SERIAL-1": (True, False, True, True)},
        {"SERIAL-1": {"DefaultChargeLevel": 32, "phase_switch_config": "auto"}},
    )
    coord.evse_runtime.async_resolve_charge_modes.assert_not_awaited()
    coord._async_resolve_green_battery_settings.assert_not_awaited()  # noqa: SLF001
    coord._async_resolve_auth_settings.assert_not_awaited()  # noqa: SLF001
    coord._async_resolve_charger_config.assert_not_awaited()  # noqa: SLF001
    assert phase_timings == {}


@pytest.mark.asyncio
async def test_update_data_raises_when_parallel_evse_lookup_fails(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()
    coord._has_successful_refresh = True  # noqa: SLF001
    coord.refresh_runner.async_run_refresh_plan = AsyncMock()  # type: ignore[method-assign]
    coord.client.status = AsyncMock(
        return_value={
            "evChargerData": [
                {
                    "sn": "SERIAL-1",
                    "name": "Garage EV",
                    "connectors": [{}],
                    "session_d": {},
                    "sch_d": {},
                    "charging": False,
                }
            ],
            "ts": 1_700_000_000,
        }
    )
    coord.evse_runtime.async_resolve_charge_modes = AsyncMock(
        return_value={"SERIAL-1": "IMMEDIATE"}
    )
    coord.evse_runtime.async_resolve_green_battery_settings = AsyncMock(return_value={})
    coord.evse_runtime.async_resolve_auth_settings = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    coord.evse_runtime.async_resolve_charger_config = AsyncMock(return_value={})

    with pytest.raises(RuntimeError, match="boom"):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_refresh_runner_login_wall_without_block_still_raises_auth_failed(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.api import EnphaseLoginWallUnauthorized

    coord = coordinator_factory()
    coord._activate_auth_block_from_login_wall = MagicMock(return_value=False)  # type: ignore[method-assign]  # noqa: SLF001

    async def _raise() -> None:
        raise EnphaseLoginWallUnauthorized(
            endpoint="/service/test",
            request_label="GET /service/test",
            status=200,
            content_type="text/html; charset=utf-8",
            body_preview_redacted="<!DOCTYPE html>",
        )

    with pytest.raises(ConfigEntryAuthFailed):
        await coord.refresh_runner.async_run_refresh_call("k", "label", _raise)


@pytest.mark.asyncio
async def test_refresh_runner_does_not_swallow_config_entry_auth_failed(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    async def _raise() -> None:
        raise ConfigEntryAuthFailed("reauth")

    with pytest.raises(ConfigEntryAuthFailed, match="reauth"):
        await coord.refresh_runner.async_run_refresh_call("k", "label", _raise)


@pytest.mark.asyncio
async def test_refresh_runner_tracks_skipped_endpoint_failure(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    async def _raise() -> None:
        raise aiohttp.ClientError("inventory down")

    timing_key, duration = await coord.refresh_runner.async_run_refresh_call(
        "devices_inventory_s",
        "device inventory",
        _raise,
        endpoint_family="inventory_topology",
    )

    health = coord.diagnostics.endpoint_family_health_diagnostics()[
        "inventory_topology"
    ]
    assert timing_key == "devices_inventory_s"
    assert duration is not None
    assert health["consecutive_failures"] == 1
    assert health["last_error"] == "inventory down"


@pytest.mark.asyncio
async def test_refresh_runner_surfaces_unexpected_stage_errors(
    coordinator_factory,
) -> None:
    coord = coordinator_factory()

    async def _raise() -> None:
        raise RuntimeError("programming bug")

    with pytest.raises(RuntimeError, match="programming bug"):
        await coord.refresh_runner.async_run_refresh_call(
            "devices_inventory_s",
            "device inventory",
            _raise,
        )

    assert coord.last_failure_source == "refresh_stage"
    assert coord.last_failure_endpoint == "devices_inventory_s"
