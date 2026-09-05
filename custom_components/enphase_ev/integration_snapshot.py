"""Immutable publication state for the Enphase coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, cast

from .auth_refresh_state import AuthRefreshSnapshot
from .evse_state import EvseControlSnapshot
from .feature_snapshot import FeatureSnapshot
from .current_power_runtime import CurrentPowerSample
from .evse_feature_flags_runtime import EvseFeatureFlagsSnapshot
from .snapshot_helpers import freeze_snapshot_mapping
from .vpp_runtime import VppSnapshot

type ChargerPayload = Mapping[str, object]
type ChargerPayloads = Mapping[str, ChargerPayload]


def freeze_charger_data(
    data: Mapping[str, Mapping[str, object]],
) -> ChargerPayloads:
    """Freeze semantic charger data once, excluding acquisition-only metadata."""

    return cast(
        ChargerPayloads,
        freeze_snapshot_mapping(
            {
                str(serial): {
                    key: value
                    for key, value in payload.items()
                    if key != "fetched_at_utc"
                }
                for serial, payload in data.items()
            }
        ),
    )


@dataclass(frozen=True, slots=True)
class IntegrationSnapshot:
    """State used to decide whether coordinator listeners need an update.

    ``revision`` is observational metadata and is deliberately excluded from
    equality. Equality is based on the normalized charger payload and explicit
    runtime-manager state, so an unchanged cloud response no longer hides a
    manager-owned state transition.
    """

    chargers: ChargerPayloads
    evse_feature_flags: EvseFeatureFlagsSnapshot
    current_power: CurrentPowerSample
    vpp: VppSnapshot = field(default_factory=VppSnapshot)
    evse: EvseControlSnapshot = field(default_factory=EvseControlSnapshot)
    auth: AuthRefreshSnapshot = field(default_factory=AuthRefreshSnapshot)
    health: tuple[object, ...] = ()
    features: FeatureSnapshot = field(default_factory=FeatureSnapshot)
    runtime_revisions: tuple[tuple[str, int], ...] = ()
    revision: int = field(default=0, compare=False)


class CoordinatorData(dict[str, dict[str, object]]):
    """Dictionary-compatible coordinator data with aggregate equality.

    Entity platforms and compatibility tests still receive the historical dict
    interface while Home Assistant compares the complete integration snapshot.
    """

    __slots__ = ("snapshot",)

    def __init__(
        self,
        data: Mapping[str, Mapping[str, object]],
        snapshot: IntegrationSnapshot,
    ) -> None:
        super().__init__((str(key), dict(value)) for key, value in data.items())
        self.snapshot = snapshot

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CoordinatorData):
            return self.snapshot == other.snapshot
        return super().__eq__(other)

    def __ne__(self, other: object) -> bool:
        return not self == other
