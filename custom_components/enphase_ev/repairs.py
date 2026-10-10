"""Recover overdue battery profile changes through Home Assistant Repairs."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .api import Unauthorized
from .battery_profile_mismatch import ISSUE_PROFILE_MISMATCH
from .const import DOMAIN, ISSUE_BATTERY_PROFILE_PENDING
from .coordinator import EnphaseCoordinator
from .runtime_data import loaded_runtime_data


class BatteryProfileRepairFlow(RepairsFlow):  # type: ignore[misc]
    """Check or cancel the specific overdue request that raised the repair."""

    def __init__(self, entry_id: str | None, requested_at: str | None) -> None:
        self._entry_id = entry_id
        self._requested_at = requested_at

    def _current_request(
        self,
    ) -> tuple[EnphaseCoordinator | None, RepairsFlowResult | None]:
        entry = (
            self.hass.config_entries.async_get_entry(self._entry_id)
            if self._entry_id
            else None
        )
        runtime = (
            loaded_runtime_data(entry) if entry and entry.domain == DOMAIN else None
        )
        if runtime is None or not runtime.coordinator.runtime_active:
            return None, self.async_abort(reason="entry_unavailable")
        coord = runtime.coordinator
        if not coord.battery_profile_pending:
            return None, self.async_create_entry(data={})
        requested_at = coord.battery_pending_requested_at
        if requested_at is None or requested_at.isoformat() != self._requested_at:
            return None, self.async_abort(reason="request_changed")
        return coord, None

    def _menu(self, step_id: str = "init") -> RepairsFlowResult:
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        return self.async_show_menu(
            step_id=step_id,
            menu_options=["check_again", "cancel_change"],
            description_placeholders=issue.translation_placeholders if issue else None,
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Offer a fresh check or an explicit cancellation."""
        _coord, result = self._current_request()
        return result if result is not None else self._menu()

    # Result menus use the same routing as the initial menu.
    async_step_pending = async_step_init
    async_step_check_failed = async_step_init

    async def async_step_check_again(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Bypass caches without repeating the profile write."""
        coord, result = self._current_request()
        if result is not None:
            return result
        assert coord is not None
        check_failed = False
        try:
            refreshed = await coord.battery_runtime.async_refresh_battery_settings(
                force=True
            )
            current, result = self._current_request()
            if result is not None:
                coord.publish_runtime_state_update("system_profile")
                return result
            if current is not coord:
                return self.async_abort(reason="entry_unavailable")
            await coord.battery_runtime.async_refresh_storm_guard_profile(force=True)
        except (HomeAssistantError, aiohttp.ClientError, TimeoutError):
            check_failed = True
            refreshed = False
        coord.publish_runtime_state_update("system_profile")
        _coord, result = self._current_request()
        if result is not None:
            return result
        return self._menu(
            "check_failed" if check_failed or not refreshed else "pending"
        )

    async def async_step_cancel_change(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Confirm cancellation before calling the existing guarded write path."""
        coord, result = self._current_request()
        if result is not None:
            return result
        assert coord is not None
        errors = {}
        if user_input is not None:
            try:
                await coord.battery_runtime.async_cancel_pending_profile_change(
                    expected_requested_at=coord.battery_pending_requested_at
                )
            except (
                HomeAssistantError,
                aiohttp.ClientError,
                TimeoutError,
                Unauthorized,
            ):
                _coord, result = self._current_request()
                if result is not None:
                    return result
                errors["base"] = "cannot_cancel"
            else:
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="cancel_change", data_schema=vol.Schema({}), errors=errors
        )


class BatteryProfileMismatchRepairFlow(RepairsFlow):  # type: ignore[misc]
    """Check disagreement or explicitly retry the displayed live profile."""

    def __init__(self, entry_id: str, identity: str) -> None:
        self._entry_id = entry_id
        self._identity = identity
        self._coord: EnphaseCoordinator | None = None
        self._target: tuple[str, int, str | None] | None = None

    def _current(
        self,
        *,
        sync_mismatch: bool = True,
    ) -> tuple[EnphaseCoordinator | None, RepairsFlowResult | None]:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        runtime = (
            loaded_runtime_data(entry) if entry and entry.domain == DOMAIN else None
        )
        if runtime is None or not runtime.coordinator.runtime_active:
            return None, self.async_abort(reason="entry_unavailable")
        coord = runtime.coordinator
        if self._coord is not None and self._coord is not coord:
            return None, self.async_abort(reason="entry_unavailable")
        self._coord = coord
        mismatch = coord.battery_runtime.profile_mismatch
        if sync_mismatch:
            mismatch.sync()
        if mismatch.identity is None:
            return None, self.async_create_entry(data={})
        if mismatch.identity != self._identity:
            return None, self.async_abort(reason="request_changed")
        return coord, None

    def _menu(self, step_id: str = "init") -> RepairsFlowResult:
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        return self.async_show_menu(
            step_id=step_id,
            menu_options=["check_again", "retry_profile"],
            description_placeholders=issue.translation_placeholders if issue else None,
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        _coord, result = self._current()
        return result if result is not None else self._menu()

    async_step_pending = async_step_init
    async_step_check_failed = async_step_init

    async def _refresh(self) -> RepairsFlowResult | None:
        coord, result = self._current()
        if result is not None:
            return result
        assert coord is not None
        runtime = coord.battery_runtime
        configured_reads = runtime.profile_mismatch.configured_reads
        live_reads = runtime.profile_mismatch.live_reads
        await runtime.async_refresh_storm_guard_profile(force=True)
        # Compare the sources only once both fresh reads have completed. Still
        # reject unloads, replacement runtimes and invalidated write contexts.
        _coord, result = self._current(sync_mismatch=False)
        if result is not None:
            return result
        await runtime.async_refresh_battery_status(force=True)
        coord.publish_runtime_state_update("system_profile")
        mismatch = runtime.profile_mismatch
        both_fresh = (
            mismatch.configured_reads > configured_reads
            and mismatch.live_reads > live_reads
        )
        _coord, result = self._current(sync_mismatch=both_fresh)
        if result is not None:
            return result
        if not both_fresh:
            return self._menu("check_failed")
        return None

    async def async_step_check_again(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        result = await self._refresh()
        return result if result is not None else self._menu("pending")

    async def async_step_retry_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        coord, result = self._current()
        if result is not None:
            return result
        assert coord is not None
        runtime = coord.battery_runtime
        profile = runtime.profile_mismatch.live
        assert profile is not None
        target = (
            profile,
            runtime.target_reserve_for_profile(profile),
            runtime.target_operation_mode_sub_type(profile),
        )
        if self._target is None:
            self._target = target
        elif target != self._target:
            return self.async_abort(reason="request_changed")
        errors = {}
        if user_input is not None:
            result = await self._refresh()
            if result is not None:
                return result
            try:
                alert_reads = runtime.storm_alert_reads
                await coord.async_refresh_storm_alert(force=True, raise_on_error=True)
                _coord, result = self._current()
                if result is not None:
                    return result
                if runtime.storm_alert_reads == alert_reads:
                    raise HomeAssistantError("Storm Alert state is unavailable")
                if (
                    runtime.target_reserve_for_profile(profile) != self._target[1]
                    or runtime.target_operation_mode_sub_type(profile)
                    != self._target[2]
                ):
                    return self.async_abort(reason="request_changed")
                await runtime.async_retry_mismatched_profile(
                    profile_key=profile,
                    reserve=self._target[1],
                    sub_type=self._target[2],
                    identity=self._identity,
                )
            except (
                HomeAssistantError,
                aiohttp.ClientError,
                TimeoutError,
                Unauthorized,
            ):
                _coord, result = self._current()
                if result is not None:
                    return result
                errors["base"] = "cannot_retry"
            else:
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="retry_profile",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "profile": runtime.battery_profile_label(profile, hass=self.hass)
                or profile,
                "reserve": str(self._target[1]),
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Route the repair using entry and request identity stored by diagnostics."""
    if data and issue_id.startswith(f"{ISSUE_PROFILE_MISMATCH}_"):
        entry_id = data.get("entry_id")
        identity = data.get("identity")
        if isinstance(entry_id, str) and isinstance(identity, str):
            return BatteryProfileMismatchRepairFlow(entry_id, identity)
    if data and (
        issue_id == ISSUE_BATTERY_PROFILE_PENDING
        or issue_id.startswith(f"{ISSUE_BATTERY_PROFILE_PENDING}_")
    ):
        entry_id = data.get("entry_id")
        requested_at = data.get("requested_at")
        if isinstance(entry_id, str) and isinstance(requested_at, str):
            return BatteryProfileRepairFlow(entry_id, requested_at)
    return BatteryProfileRepairFlow(None, None)
