"""Publish VPP event boundaries and freshness expiry without cloud polling."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar, cast

from homeassistant.core import callback as ha_callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import EnphaseCoordinator

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])
callback = cast(Callable[[_CallbackT], _CallbackT], ha_callback)


class VppCoordinatorEntity(CoordinatorEntity[EnphaseCoordinator]):  # type: ignore[misc]
    """Keep time-dependent VPP state current between coordinator updates."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        super().__init__(coordinator)
        self._cancel_vpp_transition: Callable[[], None] | None = None

    @callback
    def _schedule_vpp_transition(self) -> None:
        if self._cancel_vpp_transition is not None:
            self._cancel_vpp_transition()
            self._cancel_vpp_transition = None
        deadline = self.coordinator.vpp_runtime.next_transition_utc()
        if deadline is None:
            return

        @callback
        def transition(_now: datetime) -> None:
            self._cancel_vpp_transition = None
            # An unchanged successful response can extend freshness without
            # notifying listeners. Re-evaluate the deadline before publishing.
            self._schedule_vpp_transition()
            self.async_write_ha_state()

        self._cancel_vpp_transition = async_track_point_in_utc_time(
            self.hass, transition, deadline
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_vpp_transition()

    async def async_will_remove_from_hass(self) -> None:
        if self._cancel_vpp_transition is not None:
            self._cancel_vpp_transition()
            self._cancel_vpp_transition = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_vpp_transition()
        super()._handle_coordinator_update()
