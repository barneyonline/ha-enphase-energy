"""Optional site metadata and account roles, without retaining personal data."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from .runtime_helpers import coerce_optional_text

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

ACCESS_FLAGS = {
    "administrator": "isAdmin",
    "installer": "isInstaller",
    "host": "isHost",
    "consumption_data": "hasConsumptionDataAccess",
}
ACCESS_PRIORITY = ("administrator", "installer", "owner", "host", "consumption_data")


class CloudMetadataRuntime:
    """Refresh low-frequency metadata independently of device capabilities."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self._next_refresh = 0.0
        self._refresh_generation = 0

    def refresh_due(self) -> bool:
        return (
            self.coordinator.endpoint_manual_bypass_active()
            or time.monotonic() >= self._next_refresh
        )

    async def async_refresh(self) -> None:
        if not self.refresh_due():
            return
        self._refresh_generation += 1
        generation = self._refresh_generation
        # Set the retry deadline before I/O so optional failures cannot hot-loop.
        self._next_refresh = time.monotonic() + 900
        bootstrap, summary = await asyncio.gather(
            self.coordinator.client.site_bootstrap(),
            self.coordinator.client.system_dashboard_summary(allow_reauth=False),
            return_exceptions=True,
        )
        # A manual read may finish while the older startup warmup is still running.
        if generation != self._refresh_generation:
            return
        state: dict[str, object] = {}
        if isinstance(bootstrap, dict):
            app = bootstrap.get("app")
            if isinstance(app, dict):
                timezone = coerce_optional_text(app.get("timezone"))
                if timezone:
                    state["timezone"] = timezone
                user = app.get("user")
                owner = app.get("owner")
                if isinstance(user, dict):
                    roles = {
                        role: user[key]
                        for role, key in ACCESS_FLAGS.items()
                        if isinstance(user.get(key), bool)
                    }
                    if (
                        isinstance(owner, dict)
                        and owner.get("id") is not None
                        and user.get("id") is not None
                    ):
                        roles["owner"] = str(owner["id"]) == str(user["id"])
                    if len(roles) == len(ACCESS_PRIORITY):
                        state["access"] = roles
        if isinstance(summary, dict):
            for source, target in (
                ("country_code", "country"),
                ("currency_unit", "currency"),
            ):
                value = coerce_optional_text(summary.get(source))
                if value:
                    state[target] = value
        self.coordinator.inventory_state.cloud_metadata = state
        if "access" in state and all(
            key in state for key in ("timezone", "country", "currency")
        ):
            self._next_refresh = time.monotonic() + 21600
