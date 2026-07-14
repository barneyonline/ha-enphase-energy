from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

from .const import OPT_BATTERY_SCHEDULES_ENABLED

CallbackFactory = Callable[[Any], object]
BoundRefreshCall = tuple[str, str, Callable[[], object], str | None]

REFRESH_TASK_ENDPOINT_FAMILIES: dict[str, str] = {
    "battery_site_settings_s": "battery_site_settings",
    "battery_backup_history_s": "battery_backup_history",
    "battery_settings_s": "battery_settings",
    "battery_schedules_s": "battery_schedules",
    "storm_guard_s": "storm_guard",
    "storm_alert_s": "storm_alert",
    "tariff_s": "tariff",
    "grid_control_check_s": "grid_control_check",
    "grid_mode_status_s": "grid_mode_status",
    "grid_outage_context_s": "grid_outage_context",
    "dry_contact_settings_s": "dry_contact_settings",
    "battery_status_s": "battery_status",
    "ac_battery_devices_s": "ac_battery_devices",
    "ac_battery_telemetry_s": "ac_battery_telemetry",
    "devices_inventory_s": "inventory_topology",
    "hems_devices_s": "inventory_topology",
    "system_dashboard_s": "inventory_topology",
    "system_events_s": "system_events",
    "system_event_history_s": "system_event_history",
    "inverters_s": "inverter_inventory",
    "current_power_s": "current_power",
}

WARMUP_STAGE_DEADLINE_S = 60.0
STARTUP_POWER_DEADLINE_S = 55.0


@dataclass(frozen=True, slots=True)
class RefreshTask:
    timing_key: str
    log_label: str
    callback_factory: CallbackFactory
    endpoint_family: str | None = None


@dataclass(frozen=True, slots=True)
class RefreshStage:
    parallel_tasks: tuple[RefreshTask, ...] = ()
    ordered_tasks: tuple[RefreshTask, ...] = ()
    stage_key: str | None = None
    defer_topology: bool = False
    deadline_s: float | None = None


@dataclass(frozen=True, slots=True)
class BoundRefreshStage:
    parallel_calls: tuple[BoundRefreshCall, ...] = ()
    ordered_calls: tuple[BoundRefreshCall, ...] = ()
    stage_key: str | None = None
    defer_topology: bool = False
    deadline_s: float | None = None


@dataclass(frozen=True, slots=True)
class RefreshPlan:
    stages: tuple[RefreshStage, ...] = ()


@dataclass(frozen=True, slots=True)
class BoundRefreshPlan:
    stages: tuple[BoundRefreshStage, ...] = ()


def method_task(
    timing_key: str,
    log_label: str,
    method_name: str,
    /,
    endpoint_family: str | None = None,
    **kwargs: object,
) -> RefreshTask:
    return RefreshTask(
        timing_key=timing_key,
        log_label=log_label,
        callback_factory=lambda owner: getattr(owner, method_name)(**kwargs),
        endpoint_family=(
            endpoint_family
            if endpoint_family is not None
            else REFRESH_TASK_ENDPOINT_FAMILIES.get(timing_key)
        ),
    )


def object_method_task(
    timing_key: str,
    log_label: str,
    object_name: str,
    method_name: str,
    /,
    endpoint_family: str | None = None,
    **kwargs: object,
) -> RefreshTask:
    return RefreshTask(
        timing_key=timing_key,
        log_label=log_label,
        callback_factory=lambda owner: getattr(
            getattr(owner, object_name), method_name
        )(**kwargs),
        endpoint_family=(
            endpoint_family
            if endpoint_family is not None
            else REFRESH_TASK_ENDPOINT_FAMILIES.get(timing_key)
        ),
    )


def callback_task(
    timing_key: str,
    log_label: str,
    callback_factory: CallbackFactory,
    *,
    endpoint_family: str | None = None,
) -> RefreshTask:
    return RefreshTask(
        timing_key=timing_key,
        log_label=log_label,
        callback_factory=callback_factory,
        endpoint_family=(
            endpoint_family
            if endpoint_family is not None
            else REFRESH_TASK_ENDPOINT_FAMILIES.get(timing_key)
        ),
    )


def bind_refresh_tasks(
    owner: object, tasks: tuple[RefreshTask, ...]
) -> tuple[BoundRefreshCall, ...]:
    return tuple(
        (
            task.timing_key,
            task.log_label,
            partial(task.callback_factory, owner),
            task.endpoint_family,
        )
        for task in tasks
    )


def bind_refresh_stage(owner: object, stage: RefreshStage) -> BoundRefreshStage:
    return BoundRefreshStage(
        parallel_calls=bind_refresh_tasks(owner, stage.parallel_tasks),
        ordered_calls=bind_refresh_tasks(owner, stage.ordered_tasks),
        stage_key=stage.stage_key,
        defer_topology=stage.defer_topology,
        deadline_s=stage.deadline_s,
    )


def bind_refresh_plan(owner: object, plan: RefreshPlan) -> BoundRefreshPlan:
    return BoundRefreshPlan(
        stages=tuple(bind_refresh_stage(owner, stage) for stage in plan.stages)
    )


WARMUP_DISCOVERY_STAGE = RefreshStage(
    stage_key="discovery",
    defer_topology=True,
    deadline_s=WARMUP_STAGE_DEADLINE_S,
    parallel_tasks=(
        method_task(
            "battery_site_settings_s",
            "battery site settings",
            "_async_refresh_battery_site_settings",
        ),
    ),
    ordered_tasks=(
        object_method_task(
            "battery_status_s",
            "battery status",
            "battery_runtime",
            "async_refresh_battery_status",
        ),
        object_method_task(
            "ac_battery_devices_s",
            "AC Battery devices",
            "battery_runtime",
            "async_refresh_ac_battery_devices",
        ),
        object_method_task(
            "devices_inventory_s",
            "device inventory",
            "inventory_runtime",
            "_async_refresh_devices_inventory",
        ),
        object_method_task(
            "hems_devices_s",
            "HEMS inventory",
            "inventory_runtime",
            "_async_refresh_hems_devices",
        ),
        method_task("inverters_s", "inverters", "_async_refresh_inverters"),
    ),
)

WARMUP_STATE_STAGE = RefreshStage(
    stage_key="state",
    deadline_s=WARMUP_STAGE_DEADLINE_S,
    parallel_tasks=(
        object_method_task(
            "system_events_s",
            "system events",
            "system_events_runtime",
            "async_refresh",
        ),
        object_method_task(
            "system_event_history_s",
            "system event history",
            "system_events_runtime",
            "async_refresh_history",
        ),
        method_task(
            "battery_backup_history_s",
            "battery backup history",
            "_async_refresh_battery_backup_history",
        ),
        method_task(
            "battery_settings_s", "battery settings", "_async_refresh_battery_settings"
        ),
        object_method_task(
            "ac_battery_telemetry_s",
            "AC Battery telemetry",
            "battery_runtime",
            "async_refresh_ac_battery_telemetry",
        ),
        method_task(
            "battery_schedules_s",
            "battery schedules",
            "_async_refresh_battery_schedules",
        ),
        method_task(
            "storm_guard_s", "storm guard", "_async_refresh_storm_guard_profile"
        ),
        method_task("storm_alert_s", "storm alert", "_async_refresh_storm_alert"),
        object_method_task(
            "tariff_s",
            "tariff",
            "tariff_runtime",
            "async_refresh",
            force=True,
        ),
        object_method_task(
            "grid_control_check_s",
            "grid control",
            "battery_runtime",
            "async_refresh_grid_control_check",
        ),
        object_method_task(
            "grid_mode_status_s",
            "grid mode status",
            "battery_runtime",
            "async_refresh_grid_mode_status",
        ),
        object_method_task(
            "grid_outage_context_s",
            "grid outage context",
            "battery_runtime",
            "async_refresh_grid_outage_context",
        ),
        method_task(
            "dry_contact_settings_s",
            "dry contact settings",
            "_async_refresh_dry_contact_settings",
        ),
        method_task(
            "evse_feature_flags_s",
            "EVSE feature flags",
            "_async_refresh_evse_feature_flags",
        ),
        method_task(
            "current_power_s",
            "current power consumption",
            "_async_refresh_current_power_consumption",
        ),
    ),
)


STARTUP_POWER_STAGE = RefreshStage(
    stage_key="power",
    deadline_s=STARTUP_POWER_DEADLINE_S,
    parallel_tasks=(
        method_task(
            "current_power_s",
            "current power consumption",
            "_async_refresh_current_power_consumption",
        ),
        object_method_task(
            "site_energy_s",
            "site energy",
            "refresh_runner",
            "async_refresh_site_energy_for_warmup",
        ),
    ),
)


STARTUP_POWER_PLAN = RefreshPlan(stages=(STARTUP_POWER_STAGE,))

STARTUP_CURRENT_POWER_STAGE = RefreshStage(
    stage_key="power",
    deadline_s=STARTUP_POWER_DEADLINE_S,
    parallel_tasks=(
        method_task(
            "current_power_s",
            "current power consumption",
            "_async_refresh_current_power_consumption",
        ),
    ),
)

STARTUP_CURRENT_POWER_PLAN = RefreshPlan(stages=(STARTUP_CURRENT_POWER_STAGE,))

SITE_ONLY_FOLLOWUP_STAGE = RefreshStage(
    defer_topology=True,
    parallel_tasks=(
        object_method_task(
            "system_events_s",
            "system events",
            "system_events_runtime",
            "async_refresh",
        ),
        method_task(
            "battery_site_settings_s",
            "battery site settings",
            "_async_refresh_battery_site_settings",
        ),
        method_task(
            "battery_backup_history_s",
            "battery backup history",
            "_async_refresh_battery_backup_history",
        ),
        method_task(
            "battery_settings_s", "battery settings", "_async_refresh_battery_settings"
        ),
        method_task(
            "battery_schedules_s",
            "battery schedules",
            "_async_refresh_battery_schedules",
        ),
        method_task(
            "storm_guard_s", "storm guard", "_async_refresh_storm_guard_profile"
        ),
        method_task("storm_alert_s", "storm alert", "_async_refresh_storm_alert"),
        object_method_task(
            "tariff_s",
            "tariff",
            "tariff_runtime",
            "async_refresh",
        ),
        object_method_task(
            "grid_control_check_s",
            "grid control",
            "battery_runtime",
            "async_refresh_grid_control_check",
        ),
        object_method_task(
            "grid_mode_status_s",
            "grid mode status",
            "battery_runtime",
            "async_refresh_grid_mode_status",
        ),
        object_method_task(
            "grid_outage_context_s",
            "grid outage context",
            "battery_runtime",
            "async_refresh_grid_outage_context",
        ),
        method_task(
            "dry_contact_settings_s",
            "dry contact settings",
            "_async_refresh_dry_contact_settings",
        ),
        method_task(
            "current_power_s",
            "current power consumption",
            "_async_refresh_current_power_consumption",
        ),
    ),
    ordered_tasks=(
        object_method_task(
            "battery_status_s",
            "battery status",
            "battery_runtime",
            "async_refresh_battery_status",
        ),
        object_method_task(
            "ac_battery_devices_s",
            "AC Battery devices",
            "battery_runtime",
            "async_refresh_ac_battery_devices",
        ),
        object_method_task(
            "devices_inventory_s",
            "device inventory",
            "inventory_runtime",
            "_async_refresh_devices_inventory",
        ),
        object_method_task(
            "hems_devices_s",
            "HEMS inventory",
            "inventory_runtime",
            "_async_refresh_hems_devices",
        ),
        method_task("inverters_s", "inverters", "_async_refresh_inverters"),
    ),
)

FOLLOWUP_STAGE = RefreshStage(
    defer_topology=True,
    parallel_tasks=SITE_ONLY_FOLLOWUP_STAGE.parallel_tasks,
    ordered_tasks=(
        object_method_task(
            "battery_status_s",
            "battery status",
            "battery_runtime",
            "async_refresh_battery_status",
        ),
        object_method_task(
            "ac_battery_devices_s",
            "AC Battery devices",
            "battery_runtime",
            "async_refresh_ac_battery_devices",
        ),
        object_method_task(
            "devices_inventory_s",
            "device inventory",
            "inventory_runtime",
            "_async_refresh_devices_inventory",
        ),
        object_method_task(
            "hems_devices_s",
            "HEMS inventory",
            "inventory_runtime",
            "_async_refresh_hems_devices",
        ),
    ),
)


HEATPUMP_FOLLOWUP_STAGE = RefreshStage(
    ordered_tasks=(
        method_task(
            "heatpump_runtime_s",
            "heat pump runtime",
            "_async_refresh_heatpump_runtime_state",
        ),
        method_task(
            "heatpump_daily_s",
            "heat pump daily-consumption",
            "_async_refresh_heatpump_daily_consumption",
        ),
        method_task(
            "heatpump_power_s",
            "heat pump power",
            "_async_refresh_heatpump_power",
        ),
    ),
)


HEATPUMP_FOLLOWUP_PLAN = RefreshPlan(stages=(HEATPUMP_FOLLOWUP_STAGE,))


SITE_ONLY_FOLLOWUP_PLAN = RefreshPlan(
    stages=(SITE_ONLY_FOLLOWUP_STAGE, HEATPUMP_FOLLOWUP_STAGE)
)


FOLLOWUP_PLAN = RefreshPlan(stages=(FOLLOWUP_STAGE,))


def warmup_energy_stage(working_data: dict[str, dict[str, object]]) -> RefreshStage:
    return RefreshStage(
        stage_key="energy",
        deadline_s=WARMUP_STAGE_DEADLINE_S,
        parallel_tasks=(
            object_method_task(
                "site_energy_s",
                "site energy",
                "refresh_runner",
                "async_refresh_site_energy_for_warmup",
            ),
            callback_task(
                "evse_timeseries_s",
                "EVSE timeseries",
                lambda owner: owner.refresh_runner.async_refresh_evse_timeseries_for_warmup(
                    working_data=working_data
                ),
            ),
            callback_task(
                "sessions_s",
                "session state",
                lambda owner: owner.refresh_runner.async_refresh_session_state_for_warmup(
                    working_data=working_data
                ),
            ),
            callback_task(
                "secondary_evse_state_s",
                "secondary EVSE state",
                lambda owner: owner.refresh_runner.async_refresh_secondary_evse_state_for_warmup(
                    working_data=working_data
                ),
            ),
        ),
    )


def _warmup_task_enabled(owner: object, task: RefreshTask) -> bool:
    """Return whether a cold-start task is relevant to configured features."""

    raw_selected = getattr(owner, "_selected_type_keys", None)
    selected = (
        {str(value).strip().lower() for value in raw_selected if str(value).strip()}
        if isinstance(raw_selected, (set, list, tuple))
        else None
    )

    def _selected(type_key: str) -> bool:
        return selected is None or type_key in selected

    battery = _selected("encharge")
    evse = _selected("iqevse") and not bool(getattr(owner, "site_only", False))
    gateway = _selected("envoy")
    heatpump = _selected("heatpump")
    microinverter = _selected("microinverter") and bool(
        getattr(owner, "include_inverters", True)
    )
    entry = getattr(owner, "config_entry", None)
    options = getattr(entry, "options", {}) if entry is not None else {}
    battery_schedules = bool(
        options.get(OPT_BATTERY_SCHEDULES_ENABLED, False)
        if isinstance(options, Mapping)
        else False
    )

    key = task.timing_key
    if key in {
        "battery_site_settings_s",
        "battery_status_s",
        "ac_battery_devices_s",
        "ac_battery_telemetry_s",
        "battery_backup_history_s",
        "battery_settings_s",
        "storm_guard_s",
        "storm_alert_s",
        "grid_control_check_s",
        "grid_mode_status_s",
        "grid_outage_context_s",
    }:
        return battery
    if key == "battery_schedules_s":
        return battery and battery_schedules
    if key in {
        "hems_devices_s",
        "heatpump_runtime_s",
        "heatpump_daily_s",
        "heatpump_power_s",
    }:
        return heatpump
    if key == "inverters_s":
        return microinverter
    if key in {
        "evse_summary_s",
        "evse_feature_flags_s",
        "evse_timeseries_s",
        "sessions_s",
        "secondary_evse_state_s",
    }:
        return evse
    if key in {"system_events_s", "tariff_s", "dry_contact_settings_s"}:
        return gateway
    if key in {"current_power_s", "site_energy_s"}:
        return False
    return True


def _filter_warmup_stage(owner: object, stage: RefreshStage) -> RefreshStage | None:
    parallel = tuple(
        task for task in stage.parallel_tasks if _warmup_task_enabled(owner, task)
    )
    ordered = tuple(
        task for task in stage.ordered_tasks if _warmup_task_enabled(owner, task)
    )
    if not parallel and not ordered:
        return None
    return RefreshStage(
        parallel_tasks=parallel,
        ordered_tasks=ordered,
        stage_key=stage.stage_key,
        defer_topology=stage.defer_topology,
        deadline_s=stage.deadline_s,
    )


def warmup_plan(
    working_data: dict[str, dict[str, object]],
    *,
    owner: object | None = None,
) -> RefreshPlan:
    discovery_stage = RefreshStage(
        stage_key=WARMUP_DISCOVERY_STAGE.stage_key,
        defer_topology=WARMUP_DISCOVERY_STAGE.defer_topology,
        deadline_s=WARMUP_DISCOVERY_STAGE.deadline_s,
        parallel_tasks=(
            callback_task(
                "evse_summary_s",
                "EVSE summary",
                lambda inner_owner: inner_owner.refresh_runner.async_refresh_evse_summary_for_warmup(
                    working_data=working_data
                ),
            ),
            *WARMUP_DISCOVERY_STAGE.parallel_tasks,
        ),
        ordered_tasks=WARMUP_DISCOVERY_STAGE.ordered_tasks,
    )
    warmup_heatpump_stage = RefreshStage(
        stage_key="heatpump",
        ordered_tasks=HEATPUMP_FOLLOWUP_STAGE.ordered_tasks,
        deadline_s=WARMUP_STAGE_DEADLINE_S,
    )
    stages = (
        discovery_stage,
        WARMUP_STATE_STAGE,
        warmup_heatpump_stage,
        warmup_energy_stage(working_data),
    )
    if owner is None:
        return RefreshPlan(stages=stages)
    return _plan_from_stages(*(_filter_warmup_stage(owner, stage) for stage in stages))


def post_session_followup_stage(day_local_default: object) -> RefreshStage:
    return RefreshStage(
        defer_topology=True,
        parallel_tasks=(
            callback_task(
                "evse_timeseries_s",
                "EVSE timeseries",
                lambda owner: owner.evse_timeseries.async_refresh(
                    day_local=day_local_default
                ),
            ),
            callback_task(
                "site_energy_s",
                "site energy",
                lambda owner: owner.energy._async_refresh_site_energy(),
            ),
            method_task("inverters_s", "inverters", "_async_refresh_inverters"),
        ),
    )


def post_session_followup_plan(day_local_default: object) -> RefreshPlan:
    return RefreshPlan(stages=(post_session_followup_stage(day_local_default),))


def _plan_from_stages(*stages: RefreshStage | None) -> RefreshPlan:
    filtered = tuple(
        stage
        for stage in stages
        if stage is not None and (stage.parallel_tasks or stage.ordered_tasks)
    )
    return RefreshPlan(stages=filtered)


def _heatpump_power_covers_dependency_refreshes(runtime: object) -> bool:
    has_type = getattr(runtime, "has_type")
    if not bool(has_type("heatpump")):
        return False
    client = getattr(runtime, "client", None)
    if getattr(client, "hems_site_supported", None) is not True:
        return False
    return True


def build_heatpump_followup_plan(
    owner: object, *, force_full: bool = False
) -> RefreshPlan:
    if force_full:
        return HEATPUMP_FOLLOWUP_PLAN
    runtime = getattr(owner, "heatpump_runtime")
    ordered: list[RefreshTask] = []
    power_due = runtime.heatpump_power_refresh_due()
    runtime_due = runtime.heatpump_runtime_state_refresh_due()
    daily_due = runtime.heatpump_daily_consumption_refresh_due()
    if power_due and _heatpump_power_covers_dependency_refreshes(runtime):
        ordered.append(
            method_task(
                "heatpump_power_s",
                "heat pump power",
                "_async_refresh_heatpump_power",
            )
        )
    else:
        if runtime_due:
            ordered.append(
                method_task(
                    "heatpump_runtime_s",
                    "heat pump runtime",
                    "_async_refresh_heatpump_runtime_state",
                )
            )
        if daily_due:
            ordered.append(
                method_task(
                    "heatpump_daily_s",
                    "heat pump daily-consumption",
                    "_async_refresh_heatpump_daily_consumption",
                )
            )
        if power_due:
            ordered.append(
                method_task(
                    "heatpump_power_s",
                    "heat pump power",
                    "_async_refresh_heatpump_power",
                )
            )
    return _plan_from_stages(RefreshStage(ordered_tasks=tuple(ordered)))


def build_followup_plan(owner: object, *, force_full: bool = False) -> RefreshPlan:
    if force_full:
        return FOLLOWUP_PLAN
    battery = getattr(owner, "battery_runtime")
    inventory = getattr(owner, "inventory_runtime")
    current_power = getattr(owner, "current_power_runtime")
    evse_feature_flags = getattr(owner, "evse_feature_flags_runtime")
    system_events = getattr(owner, "system_events_runtime", None)
    parallel: list[RefreshTask] = []
    ordered: list[RefreshTask] = []
    if battery.battery_site_settings_refresh_due():
        parallel.append(
            method_task(
                "battery_site_settings_s",
                "battery site settings",
                "_async_refresh_battery_site_settings",
            )
        )
    if battery.battery_backup_history_refresh_due():
        parallel.append(
            method_task(
                "battery_backup_history_s",
                "battery backup history",
                "_async_refresh_battery_backup_history",
            )
        )
    if battery.battery_settings_refresh_due():
        parallel.append(
            method_task(
                "battery_settings_s",
                "battery settings",
                "_async_refresh_battery_settings",
            )
        )
    if battery.battery_schedules_refresh_due():
        parallel.append(
            method_task(
                "battery_schedules_s",
                "battery schedules",
                "_async_refresh_battery_schedules",
            )
        )
    if battery.storm_guard_refresh_due():
        parallel.append(
            method_task(
                "storm_guard_s",
                "storm guard",
                "_async_refresh_storm_guard_profile",
            )
        )
    if battery.storm_alert_refresh_due():
        parallel.append(
            method_task(
                "storm_alert_s",
                "storm alert",
                "_async_refresh_storm_alert",
            )
        )
    tariff = getattr(owner, "tariff_runtime")
    if tariff.refresh_due():
        parallel.append(
            object_method_task(
                "tariff_s",
                "tariff",
                "tariff_runtime",
                "async_refresh",
            )
        )
    if battery.grid_control_check_refresh_due():
        parallel.append(
            object_method_task(
                "grid_control_check_s",
                "grid control",
                "battery_runtime",
                "async_refresh_grid_control_check",
            )
        )
    if battery.grid_mode_status_refresh_due():
        parallel.append(
            object_method_task(
                "grid_mode_status_s",
                "grid mode status",
                "battery_runtime",
                "async_refresh_grid_mode_status",
            )
        )
    if battery.grid_outage_context_refresh_due():
        parallel.append(
            object_method_task(
                "grid_outage_context_s",
                "grid outage context",
                "battery_runtime",
                "async_refresh_grid_outage_context",
            )
        )
    if battery.dry_contact_settings_refresh_due():
        parallel.append(
            method_task(
                "dry_contact_settings_s",
                "dry contact settings",
                "_async_refresh_dry_contact_settings",
            )
        )
    if current_power.refresh_due():
        parallel.append(
            method_task(
                "current_power_s",
                "current power consumption",
                "_async_refresh_current_power_consumption",
            )
        )
    if evse_feature_flags.refresh_due():
        parallel.append(
            method_task(
                "evse_feature_flags_s",
                "EVSE feature flags",
                "_async_refresh_evse_feature_flags",
            )
        )
    if system_events is not None and system_events.refresh_due():
        parallel.append(
            object_method_task(
                "system_events_s",
                "system events",
                "system_events_runtime",
                "async_refresh",
            )
        )
    history_refresh_due = getattr(system_events, "history_refresh_due", None)
    if callable(history_refresh_due) and history_refresh_due():
        parallel.append(
            object_method_task(
                "system_event_history_s",
                "system event history",
                "system_events_runtime",
                "async_refresh_history",
            )
        )
    if battery.battery_status_refresh_due():
        ordered.append(
            object_method_task(
                "battery_status_s",
                "battery status",
                "battery_runtime",
                "async_refresh_battery_status",
            )
        )
    if battery.ac_battery_devices_refresh_due():
        ordered.append(
            object_method_task(
                "ac_battery_devices_s",
                "AC Battery devices",
                "battery_runtime",
                "async_refresh_ac_battery_devices",
            )
        )
    if inventory.devices_inventory_refresh_due():
        ordered.append(
            object_method_task(
                "devices_inventory_s",
                "device inventory",
                "inventory_runtime",
                "_async_refresh_devices_inventory",
            )
        )
    if inventory.hems_devices_refresh_due():
        ordered.append(
            object_method_task(
                "hems_devices_s",
                "HEMS inventory",
                "inventory_runtime",
                "_async_refresh_hems_devices",
            )
        )
    return _plan_from_stages(
        RefreshStage(
            defer_topology=True,
            parallel_tasks=tuple(parallel),
            ordered_tasks=tuple(ordered),
        )
    )


def build_site_only_followup_plan(
    owner: object, *, force_full: bool = False
) -> RefreshPlan:
    if force_full:
        return SITE_ONLY_FOLLOWUP_PLAN
    normal = build_followup_plan(owner, force_full=False)
    stages: tuple[RefreshStage, ...] = normal.stages
    inventory = getattr(owner, "inventory_runtime")
    if inventory.inverters_refresh_due():
        if stages:
            base_stage = stages[0]
            stages = (
                RefreshStage(
                    defer_topology=base_stage.defer_topology,
                    stage_key=base_stage.stage_key,
                    parallel_tasks=base_stage.parallel_tasks,
                    ordered_tasks=base_stage.ordered_tasks
                    + (
                        method_task(
                            "inverters_s",
                            "inverters",
                            "_async_refresh_inverters",
                        ),
                    ),
                ),
            )
        else:
            stages = (
                RefreshStage(
                    defer_topology=True,
                    ordered_tasks=(
                        method_task(
                            "inverters_s",
                            "inverters",
                            "_async_refresh_inverters",
                        ),
                    ),
                ),
            )
    heatpump = build_heatpump_followup_plan(owner, force_full=False)
    return _plan_from_stages(*(stages + heatpump.stages))


def build_post_session_followup_plan(
    owner: object,
    day_local_default: object,
    *,
    force_full: bool = False,
) -> RefreshPlan:
    if force_full:
        return post_session_followup_plan(day_local_default)
    parallel: list[RefreshTask] = []
    evse_timeseries = getattr(owner, "evse_timeseries")
    if evse_timeseries.refresh_due(day_local=day_local_default):
        parallel.append(
            callback_task(
                "evse_timeseries_s",
                "EVSE timeseries",
                lambda inner_owner: inner_owner.evse_timeseries.async_refresh(
                    day_local=day_local_default
                ),
            )
        )
    energy = getattr(owner, "energy")
    if energy.site_energy_refresh_due():
        parallel.append(
            callback_task(
                "site_energy_s",
                "site energy",
                lambda inner_owner: inner_owner.energy._async_refresh_site_energy(),
            )
        )
    inventory = getattr(owner, "inventory_runtime")
    if inventory.inverters_refresh_due():
        parallel.append(
            method_task("inverters_s", "inverters", "_async_refresh_inverters")
        )
    return _plan_from_stages(
        RefreshStage(
            defer_topology=True,
            parallel_tasks=tuple(parallel),
        )
    )
