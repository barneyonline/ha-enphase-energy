"""Diagnostic EV charger-to-gateway connectivity from existing summary reads."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity
from .evse_gateway_connectivity import GatewayConnectivity


class GatewayConnectedBinarySensor(EnphaseBaseEntity, BinarySensorEntity):  # type: ignore[misc]
    """Show unknown, rather than disconnected, when cloud evidence is missing."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "gateway_connected"

    def __init__(self, coord: EnphaseCoordinator, sn: str) -> None:
        super().__init__(coord, sn)
        self._attr_unique_id = f"{DOMAIN}_{sn}_gateway_connected"

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
        if not super().available or snapshot is None:
            return None
        return snapshot.connected

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self._snapshot
        if snapshot is None:
            return {}
        return {
            "gateway_connection_count": snapshot.gateway_count,
            "gateway_connected_count": snapshot.connected_count,
            "gateway_last_connection_at": snapshot.last_connection,
            "telemetry_received_at": snapshot.received_at,
            "charger_last_reported_at": snapshot.reported_at,
        }
