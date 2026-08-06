"""Sensors for the next actionable VPP/ELRP event."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .device_info_helpers import _cloud_device_info
from .runtime_helpers import inventory_type_device_info

VPP_SENSOR_KEYS: tuple[str, ...] = (
    "vpp_next_event_start",
    "vpp_next_event_end",
    "vpp_next_event_type",
    "vpp_next_event_subtype",
    "vpp_next_event_status",
)


class _VppNextEventSensor(CoordinatorEntity, SensorEntity):  # type: ignore[misc]
    """Base sensor reading the next actionable VPP event."""

    _attr_has_entity_name = True
    _field: str

    def __init__(self, coord: EnphaseCoordinator, key: str) -> None:
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{key}"

    @property
    def available(self) -> bool:
        return bool(self._coord.vpp_runtime.available)

    @property
    def native_value(self) -> Any:
        event = self._coord.vpp_runtime.next_actionable()
        return getattr(event, self._field, None) if event is not None else None

    @property
    def device_info(self) -> object:
        info = inventory_type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)


class EnphaseVppNextEventStartSensor(_VppNextEventSensor):
    _attr_translation_key = "vpp_next_event_start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _field = "start"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "vpp_next_event_start")


class EnphaseVppNextEventEndSensor(_VppNextEventSensor):
    _attr_translation_key = "vpp_next_event_end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _field = "end"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "vpp_next_event_end")


class EnphaseVppNextEventTypeSensor(_VppNextEventSensor):
    _attr_translation_key = "vpp_next_event_type"
    _field = "event_type"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "vpp_next_event_type")


class EnphaseVppNextEventSubtypeSensor(_VppNextEventSensor):
    _attr_translation_key = "vpp_next_event_subtype"
    _field = "subtype"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "vpp_next_event_subtype")


class EnphaseVppNextEventStatusSensor(_VppNextEventSensor):
    _attr_translation_key = "vpp_next_event_status"
    _field = "status"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "vpp_next_event_status")


def vpp_sensor_entities(coord: EnphaseCoordinator) -> list[SensorEntity]:
    """Return the complete site-level VPP sensor set."""

    return [
        EnphaseVppNextEventStartSensor(coord),
        EnphaseVppNextEventEndSensor(coord),
        EnphaseVppNextEventTypeSensor(coord),
        EnphaseVppNextEventSubtypeSensor(coord),
        EnphaseVppNextEventStatusSensor(coord),
    ]
