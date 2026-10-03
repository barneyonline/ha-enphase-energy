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


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Route the repair using entry and request identity stored by diagnostics."""
    if data and (
        issue_id == ISSUE_BATTERY_PROFILE_PENDING
        or issue_id.startswith(f"{ISSUE_BATTERY_PROFILE_PENDING}_")
    ):
        entry_id = data.get("entry_id")
        requested_at = data.get("requested_at")
        if isinstance(entry_id, str) and isinstance(requested_at, str):
            return BatteryProfileRepairFlow(entry_id, requested_at)
    return BatteryProfileRepairFlow(None, None)
