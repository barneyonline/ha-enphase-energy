"""Track persistent disagreement between fresh battery profile sources."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import uuid4

from homeassistant.helpers import issue_registry as ir

from .const import BATTERY_PROFILE_PENDING_TIMEOUT_S, DOMAIN
from .runtime_data import loaded_runtime_data

if TYPE_CHECKING:  # pragma: no cover
    from .battery_runtime import BatteryRuntime

ISSUE_PROFILE_MISMATCH = "battery_profile_mismatch"


class BatteryProfileMismatch:
    """Keep actual cloud observations separate from locally confirmed intent."""

    def __init__(self, runtime: BatteryRuntime) -> None:
        self.runtime = runtime
        self.configured: str | None = None
        self.live: str | None = None
        self.configured_seen = 0.0
        self.live_seen = 0.0
        self.configured_reads = 0
        self.live_reads = 0
        self.started: float | None = None
        self.identity: str | None = None
        self._pair: tuple[str, str] | None = None
        self._reported = False

    @property
    def issue_id(self) -> str:
        entry = self.runtime.coordinator.config_entry
        assert entry is not None
        return f"{ISSUE_PROFILE_MISMATCH}_{entry.entry_id}"

    def reset(self) -> None:
        """Retire the current disagreement and any open recovery flow."""
        self.started = None
        self.identity = None
        self._pair = None
        if self._reported:
            ir.async_delete_issue(self.runtime.coordinator.hass, DOMAIN, self.issue_id)
            self._reported = False

    def invalidate(self) -> None:
        """Require new observations after an accepted write or unload."""
        self.configured = self.live = None
        self.reset()

    def note_configured(self, profile: str | None) -> None:
        self.configured = profile
        self.configured_seen = time.monotonic() if profile else 0.0
        self.configured_reads += 1

    def note_live(self, profile: str | None) -> None:
        self.live = profile
        self.live_seen = time.monotonic() if profile else 0.0
        self.live_reads += 1

    def sync(self) -> None:
        """Report only a sustained disagreement supported by recent reads."""
        coord = self.runtime.coordinator
        now = time.monotonic()
        if (
            not coord.runtime_active
            or coord.config_entry is None
            or coord.battery_profile_pending
            or self.runtime.battery_state._battery_backend_profile_update_pending
            is True
            or not self.configured
            or not self.live
            or self.configured
            not in {
                "self-consumption",
                "backup_only",
                "cost_savings",
                "ai_optimisation",
            }
            or self.live
            not in {
                "self-consumption",
                "backup_only",
                "cost_savings",
                "ai_optimisation",
            }
            or self.configured == self.live
            or now - min(self.configured_seen, self.live_seen)
            > BATTERY_PROFILE_PENDING_TIMEOUT_S
        ):
            self.reset()
            return
        pair = (self.configured, self.live)
        if self._pair != pair:
            self.reset()
            self._pair = pair
            self.started = now
            self.identity = uuid4().hex
        assert self.started is not None
        if (
            now - self.started < BATTERY_PROFILE_PENDING_TIMEOUT_S
            or min(self.configured_seen, self.live_seen) <= self.started
            or self._reported
        ):
            return
        ir.async_create_issue(
            coord.hass,
            DOMAIN,
            self.issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PROFILE_MISMATCH,
            translation_placeholders={
                "site_id": coord.site_id,
                "configured_profile": self.runtime.battery_profile_label(
                    self.configured, hass=coord.hass
                )
                or self.configured,
                "live_profile": self.runtime.battery_profile_label(
                    self.live, hass=coord.hass
                )
                or self.live,
            },
            data={"entry_id": coord.config_entry.entry_id, "identity": self.identity},
        )
        self._reported = True

    def require_current(self, identity: str) -> None:
        """Reject writes from expired, superseded or retired recovery flows."""
        self.sync()
        coord = self.runtime.coordinator
        entry = coord.config_entry
        loaded = loaded_runtime_data(entry) if entry is not None else None
        if (
            self.identity != identity
            or not self._reported
            or loaded is None
            or loaded.coordinator is not coord
        ):
            self.runtime._raise_validation(  # noqa: SLF001
                "control_change_pending",
                message="The battery profile disagreement changed.",
            )

    def diagnostics(self) -> dict[str, object]:
        return {
            "configured_profile": self.configured,
            "live_profile": self.live,
            "repair_reported": self._reported,
        }
