"""Per-array production totals from the existing microinverter snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import math
import time
from typing import Any, cast

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import callback
from .coordinator import EnphaseCoordinator
from .runtime_data import EnphaseConfigEntry
from .sensor_base import EnphaseSiteSensorEntity
from .serial_discovery import active_inverter_serials_for_cleanup
from .sensor_snapshot_helpers import parse_gateway_timestamp


def array_members(coord: EnphaseCoordinator) -> dict[str, tuple[str, ...]]:
    """Group authoritative inventory by its exact nonempty array name."""
    groups: dict[str, list[str]] = {}
    for serial in sorted(active_inverter_serials_for_cleanup(coord) or ()):
        snapshot = coord.inverter_data(serial) or {}
        name = snapshot.get("array_name")
        if isinstance(name, str) and name.strip():
            groups.setdefault(name.strip(), []).append(serial)
    return {name: tuple(serials) for name, serials in groups.items()}


def _key(name: str, power: bool) -> str:
    """Avoid collisions between labels that produce the same entity-id slug."""
    identity = hashlib.sha256(name.encode()).hexdigest()
    return f"inverter_array_{'power' if power else 'energy'}_{identity}"


class EnphaseInverterArraySensor(EnphaseSiteSensorEntity):
    """A complete sum of inverter production for one named array."""

    def __init__(
        self, coord: EnphaseCoordinator, name: str, *, power: bool = False
    ) -> None:
        super().__init__(coord, _key(name, power), "", type_key="microinverter")
        self._array_name = name
        self._power = power
        self._attr_translation_key = (
            "inverter_array_power" if power else "inverter_array_energy"
        )
        self._attr_translation_placeholders = {"array": name}
        self._attr_device_class = (
            SensorDeviceClass.POWER if power else SensorDeviceClass.ENERGY
        )
        self._attr_native_unit_of_measurement = (
            UnitOfPower.WATT if power else UnitOfEnergy.KILO_WATT_HOUR
        )
        # Membership changes can lower a lifetime sum; that is not a meter reset.
        self._attr_state_class = (
            SensorStateClass.MEASUREMENT if power else SensorStateClass.TOTAL
        )
        self._attr_suggested_display_precision = 1 if power else 3

    def _members(self) -> tuple[str, ...]:
        return array_members(self._coord).get(self._array_name, ())

    def _freshness_deadline(self) -> datetime | None:
        if not self._power:
            return None
        now = cast(datetime, dt_util.utcnow())
        members = self._members()
        freshness = self._coord._inverter_parameter_success_mono
        stamps: list[float] = []
        sample_times: list[datetime] = []
        mono = time.monotonic()
        for serial in members:
            stamp = freshness.get(serial, {}).get("power")
            if (
                type(stamp) not in (int, float)
                or not math.isfinite(stamp)
                or stamp > mono
            ):
                return now
            stamps.append(float(stamp))
            telemetry = (self._coord.inverter_data(serial) or {}).get("telemetry")
            sampled_at = (
                telemetry.get("sampled_at") if isinstance(telemetry, dict) else None
            )
            if isinstance(sampled_at, dict) and "power" in sampled_at:
                sampled = parse_gateway_timestamp(sampled_at["power"])
                if sampled is None or sampled > now:
                    return now
                sample_times.append(sampled)
        if not stamps:
            return now
        policy = self._coord._endpoint_family_policy("inverter_parameter_telemetry")
        stale_after = getattr(policy, "stale_after_s", None) or 1800
        remaining = min(stamp + stale_after - mono for stamp in stamps)
        deadline = now + timedelta(seconds=remaining)
        # A successful request can still return an old cloud measurement.
        return min(
            [deadline]
            + [sample + timedelta(seconds=stale_after) for sample in sample_times]
        )

    @property
    def available(self) -> bool:
        return bool(super().available and self.native_value is not None)

    @property
    def native_value(self) -> float | None:
        members = self._members()
        if not members:
            return None
        deadline = self._freshness_deadline()
        if deadline is not None and dt_util.utcnow() >= deadline:
            return None
        values: list[float] = []
        for serial in members:
            snapshot = self._coord.inverter_data(serial) or {}
            telemetry = snapshot.get("telemetry")
            raw = (
                (telemetry.get("power") if isinstance(telemetry, dict) else None)
                if self._power
                else snapshot.get("lifetime_production_wh")
            )
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                return None
            try:
                value = float(raw)
            except ValueError:
                return None
            if not math.isfinite(value) or value < 0:
                return None
            values.append(value)
        return round(math.fsum(values) / (1 if self._power else 1000), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"array_name": self._array_name, "inverter_count": len(self._members())}


@callback
def setup_array_sensors(
    entry: EnphaseConfigEntry,
    coord: EnphaseCoordinator,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Discover arrays independently of individual inverter entities."""
    known: set[str] = set()

    @callback
    def discover() -> None:
        names = set(array_members(coord)) - known
        if names:
            async_add_entities(
                [
                    EnphaseInverterArraySensor(coord, name, power=power)
                    for name in sorted(names)
                    for power in (False, True)
                ]
            )
            known.update(names)

    # Names and membership can change without changing the inverter serial list.
    entry.async_on_unload(coord.async_add_listener(discover))
    discover()
