"""Static DC array and AC inverter nameplate capacity sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfApparentPower, UnitOfPower
from homeassistant.helpers.entity import EntityCategory

from .coordinator import EnphaseCoordinator
from .sensor_base import EnphaseSiteSensorEntity


def capacity_sensor_keys(coord: EnphaseCoordinator) -> tuple[str, ...]:
    """Discover sensors only after their individual data requirements are met."""
    state = getattr(coord, "inventory_state", None)
    snapshot = getattr(state, "array_capacity", {})
    return tuple(
        key
        for key in ("array_size", "inverter_capacity")
        if snapshot.get(key, {}).get("value") is not None
    )


class EnphaseArrayCapacitySensor(EnphaseSiteSensorEntity):
    """A site total with per-array capacities in the same unit as the state."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 3
    _unrecorded_attributes = EnphaseSiteSensorEntity._unrecorded_attributes | {"arrays"}

    def __init__(self, coord: EnphaseCoordinator, *, inverter: bool = False) -> None:
        key = "inverter_capacity" if inverter else "array_size"
        super().__init__(coord, f"total_{key}", "", type_key="microinverter")
        self._capacity_key = key
        self._attr_translation_key = f"total_{key}"
        self._attr_device_class = (
            SensorDeviceClass.APPARENT_POWER if inverter else SensorDeviceClass.POWER
        )
        self._attr_native_unit_of_measurement = (
            UnitOfApparentPower.KILO_VOLT_AMPERE if inverter else UnitOfPower.KILO_WATT
        )

    def _snapshot(self) -> dict[str, Any]:
        state = getattr(self._coord, "inventory_state", None)
        return getattr(state, "array_capacity", {}).get(self._capacity_key, {})  # type: ignore[no-any-return]

    def _freshness_deadline(self) -> None:
        """Nameplate ratings are static metadata, not instantaneous power."""
        return None

    @property
    def available(self) -> bool:
        return bool(
            self._coord.include_inverters
            and super().available
            and self.native_value is not None
        )

    @property
    def native_value(self) -> float | None:
        return self._snapshot().get("value")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"arrays": dict(self._snapshot().get("arrays", {}))}
