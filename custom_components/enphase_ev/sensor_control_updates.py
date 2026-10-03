"""Device-scoped diagnostics for confirmed control writes."""

from __future__ import annotations

from typing import Literal, cast

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .entity import EnphaseBaseEntity
from .runtime_helpers import inventory_type_device_info
from .sensor_base import EnphaseSiteSensorEntity

# Match the devices owning the existing control entities. System Profile and
# Storm Guard belong to the gateway; reserve/settings/schedules to the battery.
_GATEWAY_CONTROLS = frozenset(
    {
        "system_profile",
        "storm_guard",
        "storm_alert_opt_out",
        "tariff",
        "grid_profile",
        "grid_mode",
    }
)
_BATTERY_CONTROLS = frozenset(
    {
        "battery_reserve",
        "savings_use_battery_after_peak",
        "charge_from_grid",
        "power_match",
        "battery_shutdown_level",
        "cfg_schedule",
        "dtg_schedule",
        "rbd_schedule",
        "battery_schedule_create",
        "battery_schedule_update",
        "battery_schedule_delete",
    }
)


def _update_status(updates: dict[str, object]) -> str:
    states = {cast(dict[str, object], update)["status"] for update in updates.values()}
    for state in ("pending", "unconfirmed", "failed"):
        if state in states:
            return state
    return "idle"


def tariff_updates_use_cloud_device(coord: EnphaseCoordinator) -> bool:
    """Match tariff ownership without reading optional gateway enrichment."""
    identifier = getattr(coord.inventory_view, "type_identifier", None)
    if callable(identifier):
        return identifier("envoy") is None
    # Older inventory adapters may expose only device_info.
    return inventory_type_device_info(coord, "envoy") is None


class EnphaseDeviceUpdateStatusSensor(EnphaseSiteSensorEntity):
    """Show progress only for the controls owned by one site device."""

    _attr_translation_key = "update_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "pending", "unconfirmed", "failed"]
    _attr_icon = "mdi:progress-clock"

    def __init__(
        self, coord: EnphaseCoordinator, type_key: Literal["envoy", "encharge", "cloud"]
    ) -> None:
        super().__init__(
            coord,
            f"{type_key}_update_status",
            "Update Status",
            None if type_key == "cloud" else type_key,
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        controls = {
            "envoy": _GATEWAY_CONTROLS,
            "encharge": _BATTERY_CONTROLS,
            None: frozenset({"tariff"}),
        }[self._type_key]
        # Tariff number entities use the cloud device until a selected gateway
        # has concrete metadata. Their diagnostic follows the same ownership.
        if self._type_key == "envoy" and tariff_updates_use_cloud_device(self._coord):
            controls = controls - {"tariff"}
        updates = {
            control: update
            for control, update in self._coord.control_updates.attributes().items()
            if control in controls
        }
        if self._type_key == "envoy":
            export = self._coord.export_limit_runtime
            if export.enabled:
                if export.pending is not None:
                    status = (
                        "unconfirmed"
                        if export.request_status == "unconfirmed"
                        else "pending"
                    )
                else:
                    status = "failed" if export.request_status == "rejected" else "idle"
                updates["export_limit"] = {**export.attributes(), "status": status}
            grid = self._coord.grid_profile_runtime
            if grid.pending_profile_id is not None and "grid_profile" not in updates:
                updates["grid_profile"] = {
                    "status": grid.status,
                    "requested_profile_id": grid.pending_profile_id,
                }
        return {"updates": updates}

    @property
    def native_value(self) -> str:
        return _update_status(
            cast(dict[str, object], self.extra_state_attributes["updates"])
        )


class EnphaseChargerUpdateStatusSensor(EnphaseBaseEntity, SensorEntity):  # type: ignore[misc]
    """Show one charger's updates, including its shared Storm Guard setting."""

    _attr_translation_key = "update_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "pending", "unconfirmed", "failed"]
    _attr_icon = "mdi:progress-clock"

    def __init__(self, coord: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coord, serial)
        self._attr_unique_id = f"{DOMAIN}_{serial}_update_status"

    @property
    def available(self) -> bool:
        """Keep local progress visible when a subsequent cloud read fails."""
        return self._has_data and (
            self._coord.last_success_utc is not None or super().available
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        updates = self._coord.control_updates.attributes(self._sn)
        # Enphase exposes this setting site-wide, but its switches are chargers.
        storm = self._coord.control_updates.attributes().get("storm_evse")
        if storm is not None:
            updates["storm_evse"] = storm
        return {"updates": updates}

    @property
    def native_value(self) -> str:
        return _update_status(
            cast(dict[str, object], self.extra_state_attributes["updates"])
        )
