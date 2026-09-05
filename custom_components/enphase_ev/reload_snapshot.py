"""Detached read-only state transferred between config-entry lifecycles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from .snapshot_helpers import freeze_snapshot_mapping

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator


def _mutable_value(value: object) -> object:
    """Restore mutable payload containers without copying lifecycle objects."""

    if isinstance(value, Mapping):
        return {key: _mutable_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_value(item) for item in value]
    if isinstance(value, frozenset):
        return {_mutable_value(item) for item in value}
    return value


@dataclass(frozen=True, slots=True)
class ReloadSnapshot:
    """Read-facing cache only; never retain sessions, managers, tasks, or locks."""

    site_id: str
    discovery: Mapping[str, object]
    chargers: Mapping[str, object]
    last_success_utc: datetime | None
    last_update_success: bool

    @classmethod
    def capture(cls, coordinator: EnphaseCoordinator) -> ReloadSnapshot:
        return cls(
            site_id=coordinator.site_id,
            discovery=freeze_snapshot_mapping(coordinator.discovery_snapshot.capture()),
            chargers=freeze_snapshot_mapping(coordinator.data or {}),
            last_success_utc=coordinator.last_success_utc,
            last_update_success=coordinator.last_update_success,
        )

    def apply(self, coordinator: EnphaseCoordinator) -> None:
        """Seed discovery and telemetry, preserving new entry configuration."""

        if coordinator.site_id != self.site_id:
            raise ValueError("Cannot restore reload state for a different site")
        configured_serials = set(coordinator.serials)
        coordinator.discovery_snapshot.apply(_mutable_value(self.discovery))
        coordinator._discovery_snapshot_loaded = True
        if coordinator.config_entry is not None:
            coordinator.apply_config_entry_data(coordinator.config_entry.data)
        chargers = cast(dict[str, dict[str, object]], _mutable_value(self.chargers))
        if coordinator.site_only:
            chargers = {}
        elif configured_serials:
            chargers = {
                serial: payload
                for serial, payload in chargers.items()
                if serial in configured_serials
            }
        coordinator.last_success_utc = self.last_success_utc
        coordinator._has_successful_refresh = True
        coordinator.async_set_updated_data(chargers)
        coordinator.last_update_success = self.last_update_success
