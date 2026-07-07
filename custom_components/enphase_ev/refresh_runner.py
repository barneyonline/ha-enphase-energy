"""Run coordinator refresh stages while isolating optional endpoint failures."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from datetime import datetime
from datetime import timezone as _tz
from typing import TYPE_CHECKING, Callable

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.util import dt as dt_util

from .api import (
    EnphaseLoginWallUnauthorized,
    InvalidPayloadError,
    OptionalEndpointUnavailable,
)
from .const import DOMAIN, DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING
from .log_redaction import redact_site_id, redact_text
from .refresh_plan import BoundRefreshCall, RefreshPlan, bind_refresh_plan, warmup_plan

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)

# Optional Enphase endpoint families should not fail the whole coordinator
# refresh when their data can be marked stale and retried later.
_SKIPPABLE_REFRESH_ERRORS = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    InvalidPayloadError,
    OptionalEndpointUnavailable,
)


class _RefreshCallCancelled(Exception):
    """Signal a child refresh cancellation that should cancel sibling tasks."""


class RefreshRunner:
    """Execute refresh plans and startup warmup on behalf of the coordinator."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self._coordinator = coordinator

    def _warmup_site_state_available(self) -> bool:
        coordinator = self._coordinator
        energy = getattr(coordinator, "energy", None)
        site_energy = (
            getattr(energy, "site_energy", None)
            if energy is not None
            else getattr(coordinator, "site_energy", None)
        )
        if isinstance(site_energy, dict) and site_energy:
            return True
        return any(
            getattr(coordinator, attr, None) is not None
            for attr in (
                "tariff_billing",
                "tariff_import_rate",
                "tariff_export_rate",
                "tariff_last_refresh_utc",
                "tariff_rates_last_refresh_utc",
            )
        )

    async def async_run_refresh_call(
        self,
        timing_key: str,
        log_label: str,
        callback_factory: Callable[[], object],
        *,
        endpoint_family: str | None = None,
    ) -> tuple[str, float | None]:
        started = time.monotonic()
        try:
            result = callback_factory()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except EnphaseLoginWallUnauthorized as err:
            if self._coordinator._activate_auth_block_from_login_wall(err):
                raise self._coordinator._auth_block_update_failed() from err
            raise ConfigEntryAuthFailed from err
        except ConfigEntryAuthFailed:
            raise
        except _SKIPPABLE_REFRESH_ERRORS as err:
            if endpoint_family is not None:
                self._coordinator._note_endpoint_family_failure(endpoint_family, err)
            _LOGGER.debug(
                "Skipping %s refresh for site %s: %s",
                log_label,
                redact_site_id(self._coordinator.site_id),
                redact_text(err, site_ids=(self._coordinator.site_id,)),
            )
        except Exception as err:
            self._coordinator.last_failure_utc = dt_util.utcnow()
            self._coordinator.last_failure_status = None
            self._coordinator.last_failure_description = (
                redact_text(err, site_ids=(self._coordinator.site_id,))
                or err.__class__.__name__
            )
            self._coordinator.last_failure_response = None
            self._coordinator.last_failure_source = "refresh_stage"
            self._coordinator.last_failure_endpoint = timing_key
            raise
        return timing_key, round(time.monotonic() - started, 3)

    async def _async_run_refresh_call_for_group(
        self,
        timing_key: str,
        log_label: str,
        callback_factory: Callable[[], object],
        *,
        endpoint_family: str | None = None,
    ) -> tuple[str, float | None]:
        try:
            return await self.async_run_refresh_call(
                timing_key,
                log_label,
                callback_factory,
                endpoint_family=endpoint_family,
            )
        except asyncio.CancelledError as err:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            raise _RefreshCallCancelled from err

    async def async_run_refresh_calls(
        self,
        phase_timings: dict[str, float],
        *,
        calls: tuple[BoundRefreshCall, ...],
        stage_key: str | None = None,
        defer_topology: bool = False,
    ) -> None:
        if defer_topology:
            self._coordinator._begin_topology_refresh_batch()

        group_started = time.monotonic()
        try:
            async with asyncio.TaskGroup() as task_group:
                tasks = [
                    task_group.create_task(
                        self._async_run_refresh_call_for_group(
                            timing_key,
                            log_label,
                            callback_factory,
                            endpoint_family=endpoint_family,
                        ),
                        name=f"{DOMAIN}_refresh_{timing_key}",
                    )
                    for (
                        timing_key,
                        log_label,
                        callback_factory,
                        endpoint_family,
                    ) in calls
                ]
        except* Exception as err_group:
            exceptions = tuple(
                err
                for err in err_group.exceptions
                if not isinstance(err, _RefreshCallCancelled)
            )
            if len(exceptions) == 1:
                raise exceptions[0] from None
            if not exceptions:
                raise asyncio.CancelledError from None
            if len(exceptions) != len(err_group.exceptions):
                raise err_group.derive(exceptions) from None
            raise
        finally:
            if defer_topology:
                self._coordinator._end_topology_refresh_batch()

        results = [task.result() for task in tasks]
        for timing_key, duration in results:
            if duration is not None:
                phase_timings[timing_key] = duration
        if stage_key is not None:
            phase_timings[f"{stage_key}_s"] = round(time.monotonic() - group_started, 3)

    async def async_run_ordered_refresh_calls(
        self,
        phase_timings: dict[str, float],
        *,
        calls: tuple[BoundRefreshCall, ...],
        stage_key: str | None = None,
        defer_topology: bool = False,
    ) -> None:
        if defer_topology:
            self._coordinator._begin_topology_refresh_batch()

        group_started = time.monotonic()
        try:
            for timing_key, log_label, callback_factory, endpoint_family in calls:
                key, duration = await self.async_run_refresh_call(
                    timing_key,
                    log_label,
                    callback_factory,
                    endpoint_family=endpoint_family,
                )
                if duration is not None:
                    phase_timings[key] = duration
        finally:
            if defer_topology:
                self._coordinator._end_topology_refresh_batch()

        if stage_key is not None:
            phase_timings[f"{stage_key}_s"] = round(time.monotonic() - group_started, 3)

    async def async_run_staged_refresh_calls(
        self,
        phase_timings: dict[str, float],
        *,
        parallel_calls: tuple[BoundRefreshCall, ...] = (),
        ordered_calls: tuple[BoundRefreshCall, ...] = (),
        stage_key: str | None = None,
        defer_topology: bool = False,
    ) -> None:
        if not parallel_calls and not ordered_calls:
            if stage_key is not None:
                phase_timings[f"{stage_key}_s"] = 0.0
            return

        if defer_topology:
            self._coordinator._begin_topology_refresh_batch()

        group_started = time.monotonic()
        try:
            if parallel_calls:
                await self.async_run_refresh_calls(
                    phase_timings,
                    calls=parallel_calls,
                )
            if ordered_calls:
                await self.async_run_ordered_refresh_calls(
                    phase_timings,
                    calls=ordered_calls,
                )
        finally:
            if defer_topology:
                self._coordinator._end_topology_refresh_batch()

        if stage_key is not None:
            phase_timings[f"{stage_key}_s"] = round(time.monotonic() - group_started, 3)

    async def async_run_refresh_plan(
        self,
        phase_timings: dict[str, float],
        *,
        plan: RefreshPlan,
    ) -> None:
        bound_plan = bind_refresh_plan(self._coordinator, plan)
        for stage in bound_plan.stages:
            await self.async_run_staged_refresh_calls(
                phase_timings,
                stage_key=stage.stage_key,
                defer_topology=stage.defer_topology,
                parallel_calls=stage.parallel_calls,
                ordered_calls=stage.ordered_calls,
            )

    async def async_startup_warmup_runner(self) -> None:
        coordinator = self._coordinator
        warmup_timings: dict[str, float] = {}
        coordinator._warmup_in_progress = True
        coordinator._warmup_last_error = None
        warmup_data = (
            {sn: dict(payload) for sn, payload in coordinator.data.items()}
            if isinstance(coordinator.data, dict)
            else {}
        )
        try:
            await self.async_run_refresh_plan(
                warmup_timings,
                plan=warmup_plan(warmup_data),
            )
            if warmup_data or self._warmup_site_state_available():
                coordinator.async_set_updated_data(warmup_data)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            coordinator._warmup_last_error = (
                redact_text(err, site_ids=(coordinator.site_id,))
                or err.__class__.__name__
            )
            _LOGGER.debug(
                "Startup warmup failed for site %s: %s",
                redact_site_id(coordinator.site_id),
                redact_text(err, site_ids=(coordinator.site_id,)),
                exc_info=True,
            )
        finally:
            coordinator._warmup_in_progress = False
            coordinator._warmup_phase_timings = warmup_timings
            coordinator.discovery_snapshot.schedule_save()

    async def async_start_startup_warmup(self) -> None:
        coordinator = self._coordinator
        if coordinator._warmup_task is not None and not coordinator._warmup_task.done():
            return
        try:
            coordinator._warmup_task = coordinator.hass.async_create_task(
                self.async_startup_warmup_runner(),
                name=f"{DOMAIN}_warmup_site",
            )
        except TypeError:
            coordinator._warmup_task = coordinator.hass.async_create_task(
                self.async_startup_warmup_runner()
            )

    async def async_refresh_site_energy_for_warmup(self) -> None:
        coordinator = self._coordinator
        await coordinator.energy._async_refresh_site_energy()
        coordinator.discovery_snapshot.sync_site_energy_discovery_state()
        coordinator._sync_site_energy_issue()

    async def async_refresh_evse_timeseries_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]] | None = None,
    ) -> None:
        coordinator = self._coordinator
        try:
            day_local = dt_util.as_local(dt_util.now())
        except Exception:
            day_local = datetime.now(tz=_tz.utc)
        await coordinator.evse_timeseries.async_refresh(day_local=day_local)
        target = working_data
        if target is None and isinstance(coordinator.data, dict) and coordinator.data:
            target = {sn: dict(payload) for sn, payload in coordinator.data.items()}
        if target:
            coordinator.evse_timeseries.merge_charger_payloads(
                target, day_local=day_local
            )
            if working_data is None:
                coordinator.async_set_updated_data(target)

    async def async_refresh_session_state_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]] | None = None,
    ) -> None:
        coordinator = self._coordinator
        target = working_data if working_data is not None else coordinator.data
        if not isinstance(target, dict) or not target:
            return
        try:
            day_ref = dt_util.as_local(dt_util.now())
        except Exception:
            day_ref = datetime.now(tz=_tz.utc)
        updates = await coordinator._async_enrich_sessions(
            target.keys(),
            day_ref,
            in_background=False,
        )
        if not updates:
            return
        merged = (
            target
            if working_data is not None
            else {sn: dict(payload) for sn, payload in target.items()}
        )
        for sn, sessions in updates.items():
            payload = merged.get(sn)
            if payload is None:
                continue
            payload["energy_today_sessions"] = sessions
            payload["energy_today_sessions_kwh"] = coordinator._sum_session_energy(
                sessions
            )
        if working_data is None:
            coordinator.async_set_updated_data(merged)
        coordinator._sync_session_history_issue()

    async def async_refresh_secondary_evse_state_for_warmup(
        self,
        *,
        working_data: dict[str, dict[str, object]] | None = None,
    ) -> None:
        coordinator = self._coordinator
        target = working_data if working_data is not None else coordinator.data
        if not isinstance(target, dict) or not target:
            return
        serials = [sn for sn in coordinator.iter_serials() if sn]
        if not serials:
            return
        charge_modes = await coordinator.evse_runtime.async_resolve_charge_modes(
            serials
        )
        green_settings = (
            await coordinator.evse_runtime.async_resolve_green_battery_settings(serials)
        )
        auth_settings = await coordinator.evse_runtime.async_resolve_auth_settings(
            serials
        )
        charger_config = await coordinator.evse_runtime.async_resolve_charger_config(
            serials,
            keys=(DEFAULT_CHARGE_LEVEL_SETTING, PHASE_SWITCH_CONFIG_SETTING),
        )
        merged = (
            target
            if working_data is not None
            else {sn: dict(payload) for sn, payload in target.items()}
        )
        for sn in serials:
            payload = merged.get(sn)
            if payload is None:
                continue
            charge_mode_resolution = charge_modes.get(sn)
            charge_mode_value, charge_mode_source = (
                coordinator._charge_mode_resolution_parts(charge_mode_resolution)
            )
            if charge_mode_value:
                payload["charge_mode_pref"] = charge_mode_value
                if charge_mode_source is not None:
                    payload["charge_mode_pref_source"] = charge_mode_source
            if green_settings.get(sn) is not None:
                enabled, supported = green_settings[sn]
                payload["green_battery_supported"] = supported
                if supported:
                    payload["green_battery_enabled"] = enabled
            if auth_settings.get(sn) is not None:
                (
                    app_enabled,
                    rfid_enabled,
                    app_supported,
                    rfid_supported,
                ) = auth_settings[sn]
                payload["app_auth_supported"] = app_supported
                payload["rfid_auth_supported"] = rfid_supported
                payload["app_auth_enabled"] = app_enabled
                payload["rfid_auth_enabled"] = rfid_enabled
                if app_supported or rfid_supported:
                    values = [
                        value
                        for value in (app_enabled, rfid_enabled)
                        if value is not None
                    ]
                    payload["auth_required"] = any(values) if values else None
            config_values = charger_config.get(sn)
            if isinstance(config_values, dict):
                if PHASE_SWITCH_CONFIG_SETTING in config_values:
                    payload["phase_switch_config"] = config_values[
                        PHASE_SWITCH_CONFIG_SETTING
                    ]
                if DEFAULT_CHARGE_LEVEL_SETTING in config_values:
                    payload["default_charge_level_supported"] = True
                    payload["default_charge_level_supported_source"] = "charger_config"
                    payload["default_charge_level"] = config_values[
                        DEFAULT_CHARGE_LEVEL_SETTING
                    ]
                else:
                    payload["default_charge_level_supported"] = False
                    payload["default_charge_level_supported_source"] = "charger_config"
        if working_data is None:
            coordinator.async_set_updated_data(merged)


# Backward-compatible alias for older tests and patch targets.
CoordinatorRefreshRunner = RefreshRunner
