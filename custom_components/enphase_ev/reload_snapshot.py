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
    configured_serials: frozenset[str]
    last_success_utc: datetime | None
    last_update_success: bool

    @classmethod
    def capture(cls, coordinator: EnphaseCoordinator) -> ReloadSnapshot:
        return cls(
            site_id=coordinator.site_id,
            discovery=freeze_snapshot_mapping(coordinator.discovery_snapshot.capture()),
            chargers=freeze_snapshot_mapping(coordinator.data or {}),
            configured_serials=frozenset(coordinator._configured_serials),
            last_success_utc=coordinator.last_success_utc,
            last_update_success=coordinator.last_update_success,
        )

    def apply(self, coordinator: EnphaseCoordinator) -> None:
        """Seed discovery and telemetry, preserving new entry configuration."""

        if coordinator.site_id != self.site_id:
            raise ValueError("Cannot restore reload state for a different site")
        configured_serials = set(coordinator.serials)
        configured_order = list(coordinator._serial_order)
        coordinator.discovery_snapshot.apply(_mutable_value(self.discovery))
        coordinator._discovery_snapshot_loaded = True
        if coordinator.config_entry is not None:
            coordinator.apply_config_entry_data(coordinator.config_entry.data)
        # Entry serials can still contain retired chargers. An unchanged selection
        # must retain the previous lifecycle's discovery, including an empty list,
        # while waiting for fresh inventory. Explicit selection changes take effect
        # immediately and are subsequently reconciled by live discovery.
        if configured_serials == self.configured_serials:
            serial_order = list(cast(tuple[str, ...], self.discovery["serial_order"]))
        else:
            serial_order = configured_order
        if coordinator.site_only:
            serial_order = []
        coordinator._serial_order = serial_order
        coordinator.serials = set(serial_order)
        coordinator.always_update = coordinator.site_only or not coordinator.serials
        chargers = cast(dict[str, dict[str, object]], _mutable_value(self.chargers))
        chargers = {
            serial: payload
            for serial, payload in chargers.items()
            if serial in coordinator.serials
        }
        coordinator.last_success_utc = self.last_success_utc
        coordinator._has_successful_refresh = True
        coordinator.async_set_updated_data(chargers)
        coordinator.last_update_success = self.last_update_success
