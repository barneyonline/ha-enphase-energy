"""Diagnostic EV charger-to-gateway connectivity from existing summary reads."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar, cast

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import callback as ha_callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity
from .evse_gateway_connectivity import GatewayConnectivity

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])
callback = cast(Callable[[_CallbackT], _CallbackT], ha_callback)


class GatewayConnectedBinarySensor(EnphaseBaseEntity, BinarySensorEntity):  # type: ignore[misc]
    """Show unknown, rather than disconnected, when cloud evidence is missing."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "gateway_connected"

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_gateway_connected"
        self._cancel_expiry: Callable[[], None] | None = None

    @property
    def _snapshot(self) -> GatewayConnectivity | None:
        return self._coord.summary.gateway_connectivity(self._sn)

    @property
    def available(self) -> bool:
        # Preserve the history's explicit unknown state during source outages.
        return True

    @property
    def is_on(self) -> bool | None:
        snapshot = self._snapshot
        if (
            not super().available
            or snapshot is None
            or not snapshot.fresh(dt_util.utcnow())
        ):
            return None
        return snapshot.connected

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self._snapshot
        if snapshot is None or not snapshot.fresh(dt_util.utcnow()):
            return {}
        return {
            "gateway_connection_count": snapshot.gateway_count,
            "gateway_connected_count": snapshot.connected_count,
            "gateway_last_connection_at": snapshot.last_connection,
            "telemetry_received_at": snapshot.received_at,
            "charger_last_reported_at": snapshot.reported_at,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_expiry()

    @callback
    def _schedule_expiry(self) -> None:
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None
        snapshot = self._snapshot
        if (
            snapshot is not None
            and snapshot.expires_at is not None
            and snapshot.fresh(dt_util.utcnow())
        ):
            self._cancel_expiry = async_track_point_in_utc_time(
                self.hass, self._expire, snapshot.expires_at
            )

    @callback
    def _expire(self, _now: datetime) -> None:
        self._cancel_expiry = None
        self._schedule_expiry()
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self._schedule_expiry()

    async def async_will_remove_from_hass(self) -> None:
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None
        await super().async_will_remove_from_hass()
