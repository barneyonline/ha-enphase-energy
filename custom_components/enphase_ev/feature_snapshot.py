"""Detached publication state for the legacy battery and inventory families.

Cache deadlines, acquisition clocks, and raw diagnostic payloads do not define
entity equality. Normalized source timestamps and control state do. Unchanged
families reuse the previous immutable mapping after a content comparison, never
an object-ID comparison: runtimes may update their nested caches in place.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from types import MappingProxyType

from .snapshot_helpers import freeze_snapshot_value
from .state_models import BatteryState, HeatpumpState, InventoryState


def _semantic_fields(
    model: type[BatteryState | HeatpumpState | InventoryState],
) -> tuple[str, ...]:
    return tuple(
        item.name for item in fields(model) if item.metadata.get("publication", True)
    )


_BATTERY_FIELDS = _semantic_fields(BatteryState)
_HEATPUMP_FIELDS = _semantic_fields(HeatpumpState)
_INVENTORY_FIELDS = _semantic_fields(InventoryState)


def _matches_frozen(value: object, frozen: object) -> bool:
    """Compare mutable source content with its detached normalized snapshot."""

    if is_dataclass(value) and not isinstance(value, type):
        names = tuple(item.name for item in fields(value))
        return (
            isinstance(frozen, Mapping)
            and frozen.keys() == set(names)
            and all(
                _matches_frozen(getattr(value, name), frozen[name]) for name in names
            )
        )
    if isinstance(value, Mapping):
        return (
            isinstance(frozen, Mapping)
            and value.keys() == frozen.keys()
            and all(_matches_frozen(item, frozen[key]) for key, item in value.items())
        )
    if isinstance(value, (list, tuple)):
        return (
            isinstance(frozen, tuple)
            and len(value) == len(frozen)
            and all(
                _matches_frozen(item, old)
                for item, old in zip(value, frozen, strict=True)
            )
        )
    return bool(value == frozen)


def _publication_value(value: object) -> object:
    """Freeze dataclass fields and containers in one detached traversal."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return MappingProxyType(
            {
                item.name: _publication_value(getattr(value, item.name))
                for item in fields(value)
            }
        )
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _publication_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_publication_value(item) for item in value)
    return freeze_snapshot_value(value)


def _capture_values(
    values: Mapping[str, object] | None, previous: Mapping[str, object]
) -> Mapping[str, object]:
    values = values or {}
    if _matches_frozen(values, previous):
        return previous
    return MappingProxyType(
        {key: _publication_value(value) for key, value in values.items()}
    )


def _capture(
    state: BatteryState | HeatpumpState | InventoryState,
    names: tuple[str, ...],
    previous: Mapping[str, object],
) -> Mapping[str, object]:
    current = {name: getattr(state, name) for name in names}
    if _matches_frozen(current, previous):
        return previous
    return MappingProxyType(
        {key: _publication_value(value) for key, value in current.items()}
    )


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """Observable state not already represented by charger or dedicated snapshots."""

    battery: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    heatpump: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    inventory: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )

    site_energy: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
    tariff: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    system_events: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )


def capture_feature_snapshot(
    battery: BatteryState,
    heatpump: HeatpumpState,
    inventory: InventoryState,
    previous: FeatureSnapshot | None = None,
    *,
    site_energy: Mapping[str, object] | None = None,
    tariff: Mapping[str, object] | None = None,
    system_events: Mapping[str, object] | None = None,
) -> FeatureSnapshot:
    """Capture manager-only transitions while reusing unchanged immutable families."""

    previous = previous if previous is not None else FeatureSnapshot()
    return FeatureSnapshot(
        battery=_capture(battery, _BATTERY_FIELDS, previous.battery),
        heatpump=_capture(heatpump, _HEATPUMP_FIELDS, previous.heatpump),
        inventory=_capture(inventory, _INVENTORY_FIELDS, previous.inventory),
        site_energy=_capture_values(site_energy, previous.site_energy),
        tariff=_capture_values(tariff, previous.tariff),
        system_events=_capture_values(system_events, previous.system_events),
    )
