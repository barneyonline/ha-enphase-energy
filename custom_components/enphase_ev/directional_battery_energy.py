"""Validate directional lifetime energy and retain the published site history."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DIRECTIONAL_BATTERY_ENERGY_FIELDS, DOMAIN
from .storage import AcknowledgedStore

_FIELDS = tuple(DIRECTIONAL_BATTERY_ENERGY_FIELDS.values())
_AGGREGATE_PAIRS = {
    "charge": ("grid_battery", "solar_battery"),
    "discharge": ("battery_home", "battery_grid"),
}
_LOGGER = logging.getLogger(__name__)


def directional_battery_store(
    hass: HomeAssistant, entry_id: str
) -> Store[dict[str, object]]:
    """Store the site's published battery history independently of entity restore."""
    return AcknowledgedStore(
        hass, 1, f"{DOMAIN}.directional_battery_energy.{entry_id}", private=True
    )


def has_directional_battery_entities(hass: HomeAssistant, site_id: str) -> bool:
    """Check if this site already has directional entity IDs to protect."""
    registry = er.async_get(hass)
    return any(
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_site_{site_id}_{flow}"
        )
        is not None
        for flow in DIRECTIONAL_BATTERY_ENERGY_FIELDS
    )


@dataclass(frozen=True, slots=True)
class _BatteryHistory:
    start: date
    reported_at: datetime
    size: int
    closed: dict[str, float]
    totals: dict[str, float]

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "reported_at": self.reported_at.isoformat(),
            "size": self.size,
            "closed": self.closed,
            "totals": self.totals,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> _BatteryHistory | None:
        start = data.get("start")
        raw_reported_at = data.get("reported_at")
        size = data.get("size")
        closed = data.get("closed")
        totals = data.get("totals")
        if (
            not isinstance(start, str)
            or (start_date := dt_util.parse_date(start)) is None
            or not isinstance(raw_reported_at, str)
            or type(size) is not int
            or size < 1
            or not isinstance(closed, dict)
            or not isinstance(totals, dict)
        ):
            return None
        try:
            reported_at = datetime.fromisoformat(raw_reported_at)
        except ValueError:
            return None
        if reported_at.tzinfo is None:
            return None
        for field in _FIELDS:
            closed_value = closed.get(field)
            total_value = totals.get(field)
            if (
                not isinstance(closed_value, int | float)
                or not isinstance(total_value, int | float)
                or isinstance(closed_value, bool)
                or isinstance(total_value, bool)
                or not 0 <= closed_value <= total_value < math.inf
            ):
                return None
        return cls(
            start_date,
            reported_at,
            size,
            {key: closed[key] for key in _FIELDS},
            {key: totals[key] for key in _FIELDS},
        )


class DirectionalBatteryEnergy:
    """Accept only independently corroborated, continuous lifetime coverage."""

    def __init__(
        self,
        coerce_bucket: Callable[[object], float | None],
        store: Store[dict[str, object]] | None,
        *,
        enabled: bool,
        has_previous_entities: Callable[[], bool] | None = None,
    ) -> None:
        self._coerce_bucket = coerce_bucket
        self._store = store
        self._enabled = enabled
        self._has_previous_entities = has_previous_entities
        self._loaded = store is None
        self._history: _BatteryHistory | None = None
        self._saved_history: _BatteryHistory | None = None

    async def async_load(self) -> None:
        """Load the whole baseline before the first publication."""
        if not self._enabled or self._loaded or self._store is None:
            return
        try:
            saved = await self._store.async_load()
        except (HomeAssistantError, OSError, KeyError, TypeError, ValueError):
            _LOGGER.warning("Unable to load directional battery energy history")
            return
        if saved is not None:
            self._history = (
                _BatteryHistory.from_dict(saved) if isinstance(saved, dict) else None
            )
            if self._history is None:
                _LOGGER.warning("Invalid directional battery energy history")
                return
        elif self._has_previous_entities is not None and self._has_previous_entities():
            # Core can also return None after renaming a corrupt storage file.
            # An existing entity may still have Recorder history to protect.
            _LOGGER.warning("Missing history for existing directional battery sensors")
            return
        self._saved_history = self._history
        self._loaded = True

    async def async_save(self) -> bool:
        """Persist the accepted group history before the coordinator notifies sensors."""
        history = self._history
        if self._store is None or history is None or history == self._saved_history:
            return True
        try:
            await self._store.async_save(history.as_dict())
        except (HomeAssistantError, OSError):
            self._history = self._saved_history
            _LOGGER.warning("Unable to save directional battery energy history")
            return False
        self._saved_history = history
        return True

    def totals(
        self,
        payload: dict[str, object],
        start: date,
        reported_at: datetime,
        *,
        accept: bool,
    ) -> dict[str, tuple[float, int]]:
        """Return all four cumulative Wh totals only for a coherent observation."""
        if not self._enabled or not self._loaded:
            return {}
        result = self._evaluate(payload, start, reported_at)
        if result is None:
            return {}
        history, count = result
        if accept:
            self._history = history
        return {field: (total, count) for field, total in history.totals.items()}

    def _evaluate(
        self, payload: dict[str, object], start: date, reported_at: datetime
    ) -> tuple[_BatteryHistory, int] | None:
        previous = self._history
        if previous is not None and (
            start != previous.start or reported_at < previous.reported_at
        ):
            return None

        series: dict[str, list[object]] = {}
        size = 0
        for field in _FIELDS:
            values = payload.get(field)
            if (
                not isinstance(values, list)
                or not values
                or (size and len(values) != size)
                or self._coerce_bucket(values[-1]) is None
            ):
                return None
            size = len(values)
            series[field] = values
        if previous is not None and size < previous.size:
            return None

        aggregates: dict[str, list[object]] = {}
        for key in _AGGREGATE_PAIRS:
            values = payload.get(key)
            if isinstance(values, list):
                if any(
                    self._coerce_bucket(value) is not None for value in values[size:]
                ):
                    return None
                aggregates[key] = values

        totals = dict.fromkeys(_FIELDS, 0.0)
        closed = totals.copy()
        idle_pairs: set[str] = set()
        reporting_started = False
        count = 0
        for index in range(size):
            if (
                previous is not None
                and index == previous.size - 1
                and totals != previous.closed
            ):
                return None
            if index == size - 1:
                closed = totals.copy()
            reported = {
                field: sample
                for field, values in series.items()
                if (sample := self._coerce_bucket(values[index])) is not None
            }
            direct = {
                key: total
                for key, values in aggregates.items()
                if index < len(values)
                and (total := self._coerce_bucket(values[index])) is not None
            }
            if not reported:
                if reporting_started or any(direct.values()):
                    return None
                continue
            if len(reported) != len(_FIELDS):
                return None
            reporting_started |= any(reported.values()) or any(direct.values())
            for key, (left, right) in _AGGREGATE_PAIRS.items():
                total = direct.get(key)
                pair_total = reported[left] + reported[right]
                if total is None and (reporting_started or index == size - 1):
                    return None
                if not total:
                    if pair_total:
                        return None
                    if total == 0 and (reporting_started or index == size - 1):
                        idle_pairs.add(key)
                    continue
                # Captured daily attribution differed by up to 38 Wh with a
                # zero direction and 3.17% with two positive directions.
                tolerance = max(50.0, total * 0.05) if pair_total else 1.0
                if abs(total - pair_total) > tolerance:
                    return None
            for field, sample in reported.items():
                totals[field] += sample
            if (
                previous is not None
                and index == previous.size - 1
                and any(
                    total < previous.totals[field] for field, total in totals.items()
                )
            ):
                # A correction in any previously counted direction invalidates
                # the group's increases, even if a later bucket compensates.
                return None
            count += 1
        for key, (left, right) in _AGGREGATE_PAIRS.items():
            if not (totals[left] or totals[right] or key in idle_pairs):
                return None
        if not all(math.isfinite(total) for total in totals.values()):
            return None
        return _BatteryHistory(start, reported_at, size, closed, totals), count
