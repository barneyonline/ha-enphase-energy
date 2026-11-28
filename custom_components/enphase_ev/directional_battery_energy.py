"""Validate directional lifetime energy and retain the published site history."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from hashlib import sha256
from sys import float_info

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DIRECTIONAL_BATTERY_ENERGY_FIELDS, DOMAIN
from .serial_entity_metadata import site_sensor_unique_id
from .storage import AcknowledgedStore

_FIELDS = tuple(DIRECTIONAL_BATTERY_ENERGY_FIELDS.values())
_AGGREGATE_PAIRS = {
    "charge": ("grid_battery", "solar_battery"),
    "discharge": ("battery_home", "battery_grid"),
}
SITE_ENERGY_SOURCE_FUTURE_SKEW_S = 60.0
_LOGGER = logging.getLogger(__name__)

type DirectionalBatterySource = tuple[date, str]


def _pairs_reconcile(aggregate_wh: float, directional_wh: float) -> bool:
    # Observed daily attribution differed by up to 3.17%.
    # One Wh also accommodates rounding in small buckets.
    tolerance = max(1.0, aggregate_wh * 0.05) if directional_wh else 1.0
    return abs(aggregate_wh - directional_wh) <= tolerance


def directional_battery_store(
    hass: HomeAssistant, site_id: str
) -> Store[dict[str, object]]:
    """Retain the baseline for Recorder statistics across same-site re-adds."""
    site_key = sha256(site_id.encode()).hexdigest()
    return AcknowledgedStore(
        hass, 1, f"{DOMAIN}.directional_battery_energy.{site_key}", private=True
    )


def has_directional_battery_entities(hass: HomeAssistant, site_id: str) -> bool:
    """Check if this site already has directional entity IDs to protect."""
    registry = er.async_get(hass)
    get_entity_id = getattr(registry, "async_get_entity_id", None)
    deleted = getattr(registry, "deleted_entities", None)
    for flow in DIRECTIONAL_BATTERY_ENERGY_FIELDS:
        unique_id = site_sensor_unique_id(site_id, flow)
        if (
            callable(get_entity_id)
            and get_entity_id("sensor", DOMAIN, unique_id) is not None
        ) or (
            isinstance(deleted, Mapping) and ("sensor", DOMAIN, unique_id) in deleted
        ):
            return True
    return False


@dataclass(frozen=True, slots=True)
class _BatteryHistory:
    start: date
    timezone: str
    reported_at: datetime
    size: int
    closed: dict[str, float]
    totals: dict[str, float]
    closed_buckets: tuple[tuple[float | None, ...], ...] = ()

    @property
    def source(self) -> DirectionalBatterySource:
        return self.start, self.timezone

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "timezone": self.timezone,
            "reported_at": self.reported_at.isoformat(),
            "size": self.size,
            "closed": self.closed,
            "totals": self.totals,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> _BatteryHistory | None:
        start = data.get("start")
        zone_name = data.get("timezone")
        raw_reported_at = data.get("reported_at")
        size = data.get("size")
        closed = data.get("closed")
        totals = data.get("totals")
        if (
            not isinstance(start, str)
            or (start_date := dt_util.parse_date(start)) is None
            or not isinstance(zone_name, str)
            or not zone_name
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
                or not 0 <= closed_value <= total_value <= float_info.max
            ):
                return None
        return cls(
            start_date,
            zone_name,
            reported_at,
            size,
            {key: closed[key] for key in _FIELDS},
            {key: totals[key] for key in _FIELDS},
        )


@dataclass(frozen=True, slots=True)
class DirectionalBatteryPairReference:
    """Trusted raw counters and recovery limits for one battery flow pair."""

    reported_at: datetime
    wh: dict[str, float]
    held: bool = False
    max_elapsed_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class DirectionalBatteryRecovery:
    """The latest source cursor and each independently trusted pair."""

    source: DirectionalBatterySource
    reported_at: datetime
    bucket_count: int
    pairs: dict[str, DirectionalBatteryPairReference]


@dataclass(frozen=True, slots=True)
class _PowerCheckpoint:
    """Source identity and trusted raw readings kept across a reload."""

    closed_signature: str | None
    recovery: DirectionalBatteryRecovery

    def as_dict(self) -> dict[str, object]:
        start, zone_name = self.recovery.source
        return {
            "start": start.isoformat(),
            "timezone": zone_name,
            "reported_at": self.recovery.reported_at.isoformat(),
            "size": self.recovery.bucket_count,
            "closed_signature": self.closed_signature,
            "pairs": {
                key: {
                    "reported_at": pair.reported_at.isoformat(),
                    "wh": pair.wh,
                    "held": pair.held,
                    "max_elapsed_seconds": pair.max_elapsed_seconds,
                }
                for key, pair in self.recovery.pairs.items()
            },
        }

    @classmethod
    def from_dict(cls, data: object) -> _PowerCheckpoint | None:
        if not isinstance(data, dict):
            return None
        start = data.get("start")
        zone_name = data.get("timezone")
        size = data.get("size")
        signature = data.get("closed_signature")
        reported_at = _checkpoint_time(data.get("reported_at"))
        raw_pairs = data.get("pairs")
        if (
            not isinstance(start, str)
            or (start_date := dt_util.parse_date(start)) is None
            or not isinstance(zone_name, str)
            or type(size) is not int
            or size < 1
            or (
                signature is not None
                and (not isinstance(signature, str) or len(signature) != 64)
            )
            or reported_at is None
            or reported_at
            > dt_util.utcnow() + timedelta(seconds=SITE_ENERGY_SOURCE_FUTURE_SKEW_S)
            or not isinstance(raw_pairs, dict)
        ):
            return None
        pairs = {}
        for aggregate, fields in _AGGREGATE_PAIRS.items():
            raw = raw_pairs.get(aggregate)
            reference = _decode_pair_reference(raw, aggregate, fields, reported_at)
            if reference is None:
                return None
            pairs[aggregate] = reference
        return cls(
            signature,
            DirectionalBatteryRecovery(
                (start_date, zone_name), reported_at, size, pairs
            ),
        )


def _checkpoint_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        result = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _decode_pair_reference(
    raw: object, aggregate: str, fields: tuple[str, str], latest: datetime
) -> DirectionalBatteryPairReference | None:
    if not isinstance(raw, dict):
        return None
    reported_at = _checkpoint_time(raw.get("reported_at"))
    wh = raw.get("wh")
    held = raw.get("held")
    max_elapsed = raw.get("max_elapsed_seconds")
    if (
        reported_at is None
        or reported_at > latest
        or not isinstance(wh, dict)
        or not isinstance(held, bool)
        or (
            max_elapsed is not None
            and (
                not isinstance(max_elapsed, (int, float))
                or isinstance(max_elapsed, bool)
                or not 0 < max_elapsed <= float_info.max
            )
        )
    ):
        return None
    counters = {}
    for field in (*fields, aggregate) if aggregate in wh else fields:
        value = wh.get(field)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0 <= value <= float_info.max
        ):
            return None
        counters[field] = float(value)
    return DirectionalBatteryPairReference(reported_at, counters, held, max_elapsed)


def _closed_signature(history: _BatteryHistory, size: int) -> str:
    closed = history.closed_buckets[: size - 1]
    return sha256(repr(closed).encode()).hexdigest()


def _legacy_recovery(saved: _BatteryHistory) -> DirectionalBatteryRecovery:
    # The saved cumulative sums can round by a fraction of a raw Wh.
    counters = {
        field: max(
            0.0,
            saved.totals[field] - saved.closed[field] - math.ulp(saved.totals[field]),
        )
        for field in _FIELDS
    }
    return DirectionalBatteryRecovery(
        saved.source,
        saved.reported_at,
        saved.size,
        {
            aggregate: DirectionalBatteryPairReference(
                saved.reported_at, {field: counters[field] for field in fields}
            )
            for aggregate, fields in _AGGREGATE_PAIRS.items()
        },
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
        site_timezone: Callable[[], str | None] | None = None,
    ) -> None:
        self._coerce_bucket = coerce_bucket
        self._store = store
        self._enabled = enabled
        self._has_previous_entities = has_previous_entities
        self._site_timezone = site_timezone
        self._zone_name: str | None = "UTC" if site_timezone is None else None
        self._zone: tzinfo | None = timezone.utc if site_timezone is None else None
        self._loaded = store is None
        self._history: _BatteryHistory | None = None
        self._saved_history: _BatteryHistory | None = None
        self._saved_power_checkpoint: _PowerCheckpoint | None = None
        self._power_source_history: _BatteryHistory | None = None
        self._pending_power_source_history: _BatteryHistory | None = None
        self._load_warning_logged = False

    def site_context_refresh_due(self) -> bool:
        """Retry once startup supplies site metadata after a lifetime read."""
        if not self._enabled or self._zone is not None or self._site_timezone is None:
            return False
        zone_name = self._site_timezone()
        return bool(zone_name and zone_name != self._zone_name)

    def _warn_load_once(self, message: str) -> None:
        if not self._load_warning_logged:
            _LOGGER.warning(message)
            self._load_warning_logged = True

    async def async_load(self) -> None:
        """Load the whole baseline before the first publication."""
        if not self._enabled:
            return
        if self._site_timezone is not None:
            zone_name = self._site_timezone()
            if zone_name != self._zone_name:
                self._zone = (
                    await dt_util.async_get_time_zone(zone_name) if zone_name else None
                )
                self._zone_name = zone_name
        if self._loaded or self._store is None:
            return
        try:
            saved = await self._store.async_load()
        except (HomeAssistantError, OSError, KeyError, TypeError, ValueError):
            self._warn_load_once("Unable to load directional battery energy history")
            return
        if saved is not None:
            self._history = (
                _BatteryHistory.from_dict(saved) if isinstance(saved, dict) else None
            )
            if self._history is None:
                self._warn_load_once("Invalid directional battery energy history")
                return
            assert isinstance(saved, dict)
            self._saved_power_checkpoint = _PowerCheckpoint.from_dict(
                saved.get("power")
            )
        elif self._has_previous_entities is not None and self._has_previous_entities():
            # Core can also return None after renaming a corrupt storage file.
            # An existing entity may still have Recorder history to protect.
            self._warn_load_once(
                "Missing history for existing directional battery sensors"
            )
            return
        self._saved_history = self._history
        self._loaded = True

    async def async_save(
        self, recovery: DirectionalBatteryRecovery | None = None
    ) -> bool:
        """Persist the accepted group history before the coordinator notifies sensors."""
        history = self._history
        checkpoint = self._saved_power_checkpoint
        power_source = self._power_source_history
        if recovery is not None:
            signature = None
            if power_source is not None and power_source.source == recovery.source:
                signature = _closed_signature(power_source, recovery.bucket_count)
            elif (
                checkpoint is not None and checkpoint.recovery.source == recovery.source
            ):
                signature = checkpoint.closed_signature
            checkpoint = _PowerCheckpoint(signature, recovery)
        if (
            self._store is None
            or history is None
            or (
                history == self._saved_history
                and checkpoint == self._saved_power_checkpoint
            )
        ):
            return True
        data = history.as_dict()
        if checkpoint is not None:
            data["power"] = checkpoint.as_dict()
        try:
            await self._store.async_save(data)
        except (HomeAssistantError, OSError):
            self._history = self._saved_history
            _LOGGER.warning("Unable to save directional battery energy history")
            return False
        self._saved_history = history
        self._saved_power_checkpoint = checkpoint
        return True

    def totals(
        self,
        payload: dict[str, object],
        start: date,
        reported_at: datetime,
        *,
        for_power: bool = False,
    ) -> dict[str, tuple[float, int]]:
        """Return all four cumulative Wh totals only for a coherent observation."""
        if not self.source_context_ready:
            return {}
        previous = self._power_source_history if for_power else self._history
        if (
            for_power
            and previous is None
            and self._saved_history is not None
            and reported_at <= self._saved_history.reported_at
        ):
            return {}
        result = self._evaluate(
            payload, start, reported_at, previous, for_power=for_power
        )
        if result is None:
            return {}
        history, count = result
        if for_power:
            if not self._accept_power_source_history(history):
                return {}
        else:
            self._history = history
        return {field: (total, count) for field, total in history.totals.items()}

    @property
    def source_context_ready(self) -> bool:
        return self._enabled and self._loaded and self._zone is not None

    @property
    def power_source(self) -> DirectionalBatterySource:
        history = self._power_source_history
        assert history is not None
        return history.source

    def _accept_power_source_history(self, history: _BatteryHistory) -> bool:
        previous = self._power_source_history
        pending = self._pending_power_source_history
        if pending is not None and history.reported_at <= pending.reported_at:
            return False
        changed = False
        if previous is not None:
            changed = (
                history.closed_buckets[: len(previous.closed_buckets)]
                != previous.closed_buckets
            )
        elif (checkpoint := self._saved_power_checkpoint) is not None:
            saved = checkpoint.recovery
            if history.reported_at <= saved.reported_at:
                return False
            changed = (
                saved.source == history.source
                and checkpoint.closed_signature is not None
                and _closed_signature(history, saved.bucket_count)
                != checkpoint.closed_signature
            )
        if changed and (
            pending is None
            or history.closed_buckets[: len(pending.closed_buckets)]
            != pending.closed_buckets
        ):
            # A new version of closed history must persist at an advancing
            # cursor. Reject this report so no window spans the correction.
            self._pending_power_source_history = history
            return False
        self._power_source_history = history
        self._pending_power_source_history = None
        return True

    def power_startup_reference(self) -> DirectionalBatteryRecovery | None:
        """Return saved same-day counters to guard a reload during a dropout."""
        current = self._power_source_history
        assert current is not None
        checkpoint = self._saved_power_checkpoint
        if (
            checkpoint is not None
            and checkpoint.recovery.source == current.source
            and current.size == checkpoint.recovery.bucket_count
        ):
            return checkpoint.recovery
        saved = self._saved_history
        if (
            saved is None
            or saved.start != current.start
            or saved.timezone != current.timezone
            or saved.size != current.size
        ):
            return None
        return _legacy_recovery(saved)

    def power_startup_for_rejected_report(self) -> DirectionalBatteryRecovery | None:
        """Retain the site clock on the last trusted source checkpoint."""
        checkpoint = self._saved_power_checkpoint
        if checkpoint is not None:
            return checkpoint.recovery
        saved = self._saved_history
        return _legacy_recovery(saved) if saved is not None else None

    def _evaluate(
        self,
        payload: dict[str, object],
        start: date,
        reported_at: datetime,
        previous: _BatteryHistory | None,
        *,
        for_power: bool,
    ) -> tuple[_BatteryHistory, int] | None:
        zone_name = self._zone_name
        zone = self._zone
        assert zone_name is not None and zone is not None
        if previous is not None and (
            start != previous.start
            or zone_name != previous.timezone
            or reported_at < previous.reported_at
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
        # The source start is site-local, and the report cursor is UTC.
        # A complete daily series includes the site's current partial bucket.
        try:
            last_day = reported_at.astimezone(zone).date()
        except OverflowError:
            return None
        if size != (last_day - start).days + 1:
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

        source_series = (*series.values(), *aggregates.values())
        totals = dict.fromkeys(_FIELDS, 0.0)
        closed = totals.copy()
        closed_buckets: list[tuple[float | None, ...]] = []
        direct_totals = dict.fromkeys(_AGGREGATE_PAIRS, 0.0)
        reporting_started = False
        count = 0
        for index in range(size):
            if for_power and any(
                isinstance(values[index], bool)
                for values in source_series
                if index < len(values)
            ):
                return None
            if (
                previous is not None
                and index == previous.size - 1
                and not for_power
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
            if for_power and index < size - 1:
                closed_buckets.append(
                    tuple(reported.get(field) for field in _FIELDS)
                    + tuple(direct.get(field) for field in _AGGREGATE_PAIRS)
                )
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
                    continue
                if not _pairs_reconcile(total, pair_total):
                    return None
                direct_totals[key] += total
            for field, sample in reported.items():
                totals[field] += sample
            if (
                previous is not None
                and index == previous.size - 1
                and not for_power
                and any(
                    total < previous.totals[field] for field, total in totals.items()
                )
            ):
                # Recorder cannot accept any decrease in already counted
                # energy. Power handles a formerly open bucket by reseeding
                # when a counter decreases or the site enters a new day.
                return None
            count += 1
        if not any(totals.values()) or not all(
            math.isfinite(total) for total in totals.values()
        ):
            return None
        for key, (left, right) in _AGGREGATE_PAIRS.items():
            pair_total = totals[left] + totals[right]
            direct_total = direct_totals[key]
            if pair_total:
                if not _pairs_reconcile(direct_total, pair_total):
                    return None
            elif direct_total:
                # An entirely inactive directional pair requires an entirely
                # inactive direct aggregate, even if each bucket was within 1 Wh.
                return None
        if (
            previous is not None
            and reported_at == previous.reported_at
            and totals != previous.totals
        ):
            return None
        return (
            _BatteryHistory(
                start,
                zone_name,
                reported_at,
                size,
                closed,
                totals,
                tuple(closed_buckets),
            ),
            count,
        )
