"""Validate directional lifetime energy and retain the published site history."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from hashlib import sha256
from sys import float_info
from types import MappingProxyType

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DIRECTIONAL_BATTERY_ENERGY_FIELDS,
    DOMAIN,
    SITE_ENERGY_DEFAULT_INTERVAL_MIN,
)
from .power_validation import energy_advance_is_plausible, power_recovery_elapsed
from .serial_entity_metadata import site_sensor_unique_id
from .storage import AcknowledgedStore

_FIELDS = tuple(DIRECTIONAL_BATTERY_ENERGY_FIELDS.values())
_AGGREGATE_PAIRS = {
    "charge": ("grid_battery", "solar_battery"),
    "discharge": ("battery_home", "battery_grid"),
}
SITE_ENERGY_SOURCE_FUTURE_SKEW_S = 60.0
# Retain the five-minute reports of a full local day, including a DST fold.
OBSERVED_POWER_REPORT_LIMIT = 300
_LOGGER = logging.getLogger(__name__)

type DirectionalBatterySource = tuple[date, str]


def _pairs_reconcile(aggregate_wh: float, directional_wh: float) -> bool:
    # Observed daily attribution differed by up to 3.17%.
    # One Wh also accommodates rounding in small buckets.
    tolerance = max(1.0, aggregate_wh * 0.05) if directional_wh else 1.0
    return abs(aggregate_wh - directional_wh) <= tolerance


def directional_battery_store(
    hass: HomeAssistant, site_id: str, *, for_power: bool = False
) -> Store[dict[str, object]]:
    """Retain the cumulative baseline or power checkpoint across same-site re-adds."""
    site_key = sha256(site_id.encode()).hexdigest()
    feature = "power" if for_power else "energy"
    return AcknowledgedStore(
        hass, 1, f"{DOMAIN}.directional_battery_{feature}.{site_key}", private=True
    )


def has_directional_battery_entities(
    hass: HomeAssistant,
    site_id: str,
    keys: Iterable[str] = DIRECTIONAL_BATTERY_ENERGY_FIELDS,
) -> bool:
    """Check if this site already has directional entity IDs to protect."""
    registry = er.async_get(hass)
    get_entity_id = getattr(registry, "async_get_entity_id", None)
    deleted = getattr(registry, "deleted_entities", None)
    for flow in keys:
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
    untrusted_power_pairs: frozenset[str] = frozenset()

    @property
    def source(self) -> DirectionalBatterySource:
        return self.start, self.timezone

    @property
    def signature(self) -> str:
        """Identify the normalized scope, clock and counters of this report."""
        fields = (
            self.source,
            self.size,
            self.reported_at.isoformat(),
            tuple(self.closed[field] for field in _FIELDS),
            tuple(self.totals[field] for field in _FIELDS),
        )
        return sha256(repr(fields).encode()).hexdigest()

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "start": self.start.isoformat(),
            "timezone": self.timezone,
            "reported_at": self.reported_at.isoformat(),
            "size": self.size,
            "closed": self.closed,
            "totals": self.totals,
        }
        if self.untrusted_power_pairs:
            data["untrusted_power_pairs"] = sorted(self.untrusted_power_pairs)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> _BatteryHistory | None:
        start = data.get("start")
        zone_name = data.get("timezone")
        raw_reported_at = data.get("reported_at")
        size = data.get("size")
        closed = data.get("closed")
        totals = data.get("totals")
        untrusted_pairs = data.get("untrusted_power_pairs", [])
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
            or not isinstance(untrusted_pairs, list)
            or any(
                not isinstance(key, str) or key not in _AGGREGATE_PAIRS
                for key in untrusted_pairs
            )
        ):
            return None
        try:
            reported_at = datetime.fromisoformat(raw_reported_at)
        except ValueError:
            return None
        if reported_at.tzinfo is None or reported_at > dt_util.utcnow() + timedelta(
            seconds=SITE_ENERGY_SOURCE_FUTURE_SKEW_S
        ):
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
            untrusted_power_pairs=frozenset(untrusted_pairs),
        )


@dataclass(frozen=True, slots=True)
class DirectionalBatteryPairReference:
    """Trusted raw counters and recovery limits for one battery flow pair."""

    reported_at: datetime
    wh: Mapping[str, float]
    held: bool = False
    max_elapsed_seconds: float | None = None
    aggregate_floor_wh: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "wh", MappingProxyType(dict(self.wh)))


@dataclass(frozen=True, slots=True)
class DirectionalBatteryRecovery:
    """The latest source cursor and each independently trusted pair."""

    source: DirectionalBatterySource
    reported_at: datetime
    bucket_count: int
    pairs: Mapping[str, DirectionalBatteryPairReference]
    observed_signatures: tuple[str, ...] = ()
    cumulative_signature: str | None = None
    untrusted_pairs: frozenset[str] = frozenset()
    lifetime_totals_wh: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "pairs", MappingProxyType(dict(self.pairs)))


@dataclass(frozen=True, slots=True)
class _PowerCheckpoint:
    """Source identity and trusted raw readings kept across a reload."""

    closed_signature: str | None
    closed_signature_size: int | None
    recovery: DirectionalBatteryRecovery

    def as_dict(self) -> dict[str, object]:
        start, zone_name = self.recovery.source
        return {
            "start": start.isoformat(),
            "timezone": zone_name,
            "reported_at": self.recovery.reported_at.isoformat(),
            "observed_signatures": list(self.recovery.observed_signatures),
            "cumulative_signature": self.recovery.cumulative_signature,
            "size": self.recovery.bucket_count,
            "closed_signature": self.closed_signature,
            "closed_signature_size": self.closed_signature_size,
            "pairs": {
                key: {
                    "reported_at": pair.reported_at.isoformat(),
                    "wh": dict(pair.wh),
                    "held": pair.held,
                    "max_elapsed_seconds": pair.max_elapsed_seconds,
                    "aggregate_floor_wh": pair.aggregate_floor_wh,
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
        signature_size = data.get(
            "closed_signature_size", size if signature is not None else None
        )
        reported_at = _checkpoint_time(data.get("reported_at"))
        observed_signatures = data.get("observed_signatures", [])
        cumulative_signature = data.get("cumulative_signature")
        raw_pairs = data.get("pairs")
        if (
            not isinstance(start, str)
            or (start_date := dt_util.parse_date(start)) is None
            or not isinstance(zone_name, str)
            or not zone_name
            or type(size) is not int
            or size < 1
            or (
                signature is not None
                and (
                    not isinstance(signature, str)
                    or len(signature) != 64
                    or type(signature_size) is not int
                    or not 1 <= signature_size <= size
                )
            )
            or reported_at is None
            or not isinstance(observed_signatures, list)
            or len(observed_signatures) > OBSERVED_POWER_REPORT_LIMIT
            or any(
                not isinstance(item, str) or len(item) != 64
                for item in observed_signatures
            )
            or (
                cumulative_signature is not None
                and (
                    not isinstance(cumulative_signature, str)
                    or len(cumulative_signature) != 64
                )
            )
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
            signature_size if signature is not None else None,
            DirectionalBatteryRecovery(
                (start_date, zone_name),
                reported_at,
                size,
                pairs,
                tuple(observed_signatures),
                cumulative_signature,
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
    aggregate_floor = raw.get("aggregate_floor_wh")
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
        or (
            aggregate_floor is not None
            and (
                isinstance(aggregate_floor, bool)
                or not isinstance(aggregate_floor, int | float)
                or not 0 <= aggregate_floor <= float_info.max
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
    return DirectionalBatteryPairReference(
        reported_at, counters, held, max_elapsed, aggregate_floor
    )


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
        cumulative_signature=saved.signature,
        untrusted_pairs=saved.untrusted_power_pairs,
        lifetime_totals_wh=MappingProxyType(saved.totals),
    )


def merge_power_recovery(
    power: DirectionalBatteryRecovery,
    cumulative: DirectionalBatteryRecovery,
) -> DirectionalBatteryRecovery:
    """Keep both trusted directional floors for the same source day."""
    if (power.source, power.bucket_count) != (
        cumulative.source,
        cumulative.bucket_count,
    ):
        return max(
            power,
            trusted_cumulative_recovery(cumulative),
            key=lambda recovery: recovery.reported_at,
        )
    pairs = dict(power.pairs)
    for aggregate, fields in _AGGREGATE_PAIRS.items():
        if aggregate in cumulative.untrusted_pairs:
            continue
        floor = cumulative.pairs[aggregate]
        previous = pairs.setdefault(aggregate, floor)
        if any(floor.wh[field] > previous.wh[field] for field in fields):
            assert cumulative.lifetime_totals_wh is not None
            elapsed = power_recovery_elapsed(
                (floor.reported_at - previous.reported_at).total_seconds(),
                previous.max_elapsed_seconds,
                SITE_ENERGY_DEFAULT_INTERVAL_MIN * 60,
            )
            if not all(
                energy_advance_is_plausible(
                    max(0, floor.wh[field] - previous.wh[field]),
                    elapsed,
                    cumulative.lifetime_totals_wh[field],
                )
                for field in fields
            ):
                pairs[aggregate] = replace(previous, max_elapsed_seconds=elapsed)
                continue
            # The merged floors may come from different reports. There is no
            # corresponding direct counter for this pair.
            pairs[aggregate] = replace(
                previous,
                reported_at=max(previous.reported_at, floor.reported_at),
                wh={
                    field: max(previous.wh[field], floor.wh[field]) for field in fields
                },
                aggregate_floor_wh=previous.wh.get(
                    aggregate, previous.aggregate_floor_wh
                ),
            )
    return replace(
        power,
        reported_at=max(power.reported_at, cumulative.reported_at),
        pairs=pairs,
        cumulative_signature=cumulative.cumulative_signature,
    )


def trusted_cumulative_recovery(
    cumulative: DirectionalBatteryRecovery,
) -> DirectionalBatteryRecovery:
    """Seed only the cumulative pairs that power has not rejected."""
    return replace(
        cumulative,
        pairs={
            name: reference
            for name, reference in cumulative.pairs.items()
            if name not in cumulative.untrusted_pairs
        },
    )


def _startup_recovery(
    power: DirectionalBatteryRecovery | None, saved: _BatteryHistory | None
) -> DirectionalBatteryRecovery | None:
    if saved is None:
        return power
    cumulative = _legacy_recovery(saved)
    if power is None:
        return trusted_cumulative_recovery(cumulative)
    if power is not None and power_observed_cumulative(power, cumulative):
        return replace(power, cumulative_signature=cumulative.cumulative_signature)
    return merge_power_recovery(power, cumulative)


def power_observed_cumulative(
    power: DirectionalBatteryRecovery, cumulative: DirectionalBatteryRecovery
) -> bool:
    """Check if power already evaluated the cumulative report's source window."""
    return (
        (power.source, power.bucket_count)
        == (cumulative.source, cumulative.bucket_count)
        and cumulative.cumulative_signature is not None
        and (
            cumulative.cumulative_signature == power.cumulative_signature
            or cumulative.cumulative_signature in power.observed_signatures
        )
    )


class DirectionalBatteryEnergy:
    """Accept only independently corroborated, continuous lifetime coverage."""

    def __init__(
        self,
        coerce_bucket: Callable[[object], float | None],
        store: Store[dict[str, object]] | None,
        *,
        enabled: bool,
        power_store: Store[dict[str, object]] | None = None,
        has_previous_entities: Callable[[], bool] | None = None,
        has_previous_power_entities: Callable[[], bool] | None = None,
        site_timezone: Callable[[], str | None] | None = None,
    ) -> None:
        self._coerce_bucket = coerce_bucket
        self._store = store
        self._power_store = power_store
        self._enabled = enabled
        self._has_previous_entities = has_previous_entities
        self._has_previous_power_entities = has_previous_power_entities
        self._site_timezone = site_timezone
        self._zone_name: str | None = "UTC" if site_timezone is None else None
        self._zone: tzinfo | None = timezone.utc if site_timezone is None else None
        self._loaded = store is None
        self._history_readable = store is None
        self._power_loaded = power_store is None
        self._history: _BatteryHistory | None = None
        self._saved_history: _BatteryHistory | None = None
        self._stored_history_record: dict[str, object] | None = None
        self._saved_power_checkpoint: _PowerCheckpoint | None = None
        self._power_checkpoint_needs_migration = False
        self._power_recovery_at_load: DirectionalBatteryRecovery | None = None
        self._power_source_history: _BatteryHistory | None = None
        self._pending_power_source_history: _BatteryHistory | None = None
        self._load_warning_logged = False
        self._power_load_warning_logged = False

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

    async def async_load(
        self,
    ) -> tuple[DirectionalBatteryRecovery, DirectionalBatteryRecovery | None] | None:
        """Load both histories and return any cumulative floor discovered late."""
        if not self._enabled:
            return None
        if self._site_timezone is not None:
            zone_name = self._site_timezone()
            if zone_name != self._zone_name:
                self._zone = (
                    await dt_util.async_get_time_zone(zone_name) if zone_name else None
                )
                self._zone_name = zone_name
        had_history = self._loaded
        power_was_loaded = self._power_loaded
        if not self._loaded and self._store is not None:
            await self._async_load_history(self._store)
        if (
            self._history_readable
            and not self._power_loaded
            and self._power_store is not None
        ):
            await self._async_load_power_checkpoint(self._power_store)
        if not had_history and power_was_loaded and self._saved_history is not None:
            return _legacy_recovery(self._saved_history), self._power_recovery_at_load
        return None

    async def _async_load_history(self, store: Store[dict[str, object]]) -> None:
        try:
            saved = await store.async_load()
        except (HomeAssistantError, OSError, KeyError, TypeError, ValueError):
            self._history_readable = False
            self._warn_load_once("Unable to load directional battery energy history")
            return
        self._history_readable = True
        self._stored_history_record = saved if isinstance(saved, dict) else None
        if saved is not None:
            if not isinstance(saved, dict):
                self._warn_load_once("Invalid directional battery energy history")
                return
            history = _BatteryHistory.from_dict(saved)
            if history is None or not await self._saved_scope_is_valid(
                history.source,
                history.size,
                (history.reported_at,),
            ):
                self._warn_load_once("Invalid directional battery energy history")
                return
            self._history = history
        elif self._has_previous_entities is not None and self._has_previous_entities():
            # Core can also return None after renaming a corrupt storage file.
            # An existing entity may still have Recorder history to protect.
            self._warn_load_once(
                "Missing history for existing directional battery sensors"
            )
            return
        self._saved_history = self._history
        self._loaded = True

    async def _async_load_power_checkpoint(
        self, store: Store[dict[str, object]]
    ) -> None:
        try:
            saved = await store.async_load()
        except (HomeAssistantError, OSError, KeyError, TypeError, ValueError):
            self._warn_power_load_once(
                "Unable to load directional battery power checkpoint"
            )
            return
        checkpoint = await self._decode_power_checkpoint(saved)
        if saved is None and self._stored_history_record is not None:
            checkpoint = await self._decode_power_checkpoint(
                self._stored_history_record.get("power")
            )
            self._power_checkpoint_needs_migration = checkpoint is not None
        if (
            checkpoint is None
            and self._has_previous_power_entities is not None
            and self._has_previous_power_entities()
        ):
            self._warn_power_load_once(
                "Missing or invalid power checkpoint for existing directional battery power sensors"
            )
            return
        if saved is not None and checkpoint is None:
            self._warn_power_load_once("Invalid directional battery power checkpoint")
        self._saved_power_checkpoint = checkpoint
        if checkpoint is not None:
            self._power_recovery_at_load = checkpoint.recovery
        self._power_loaded = True

    async def _decode_power_checkpoint(self, saved: object) -> _PowerCheckpoint | None:
        checkpoint = _PowerCheckpoint.from_dict(saved)
        if checkpoint is None:
            return None
        recovery = checkpoint.recovery
        if not await self._saved_scope_is_valid(
            recovery.source,
            recovery.bucket_count,
            (pair.reported_at for pair in recovery.pairs.values()),
        ):
            return None
        return checkpoint

    async def _saved_scope_is_valid(
        self,
        source: DirectionalBatterySource,
        bucket_count: int,
        report_times: Iterable[datetime],
    ) -> bool:
        start, zone_name = source
        zone = (
            self._zone
            if zone_name == self._zone_name
            else await dt_util.async_get_time_zone(zone_name)
        )
        if zone is None:
            return False
        try:
            return all(
                (reported_at.astimezone(zone).date() - start).days + 1 == bucket_count
                for reported_at in report_times
            )
        except OverflowError:
            return False

    def _warn_power_load_once(self, message: str) -> None:
        if not self._power_load_warning_logged:
            _LOGGER.warning(message)
            self._power_load_warning_logged = True

    async def async_save(self) -> bool:
        """Persist the cumulative baseline before publishing lifetime energy."""
        history = self._history
        if self._store is None or history is None or history == self._saved_history:
            return True
        data = history.as_dict()
        # Preserve embedded checkpoints from older builds until a new power
        # checkpoint can be written, including while power is disabled.
        if (
            self._stored_history_record is not None
            and "power" in self._stored_history_record
        ):
            data["power"] = self._stored_history_record["power"]
        try:
            await self._store.async_save(data)
        except (HomeAssistantError, OSError):
            self._history = self._saved_history
            _LOGGER.warning("Unable to save directional battery energy history")
            return False
        self._saved_history = history
        self._stored_history_record = data
        return True

    async def async_save_power(self, recovery: DirectionalBatteryRecovery) -> bool:
        """Persist the power checkpoint before publishing a window."""
        if self._power_store is None:
            return True
        previous = self._saved_power_checkpoint
        prior_recovery = (
            previous.recovery
            if previous is not None
            and (previous.recovery.source, previous.recovery.bucket_count)
            == (recovery.source, recovery.bucket_count)
            else None
        )
        if self._saved_history is not None:
            cumulative = _legacy_recovery(self._saved_history)
            if power_observed_cumulative(recovery, cumulative) or (
                prior_recovery is not None
                and power_observed_cumulative(prior_recovery, cumulative)
            ):
                recovery = replace(
                    recovery, cumulative_signature=cumulative.cumulative_signature
                )
        power_source = self._power_source_history
        signature = None
        signature_size = None
        if (
            power_source is not None
            and power_source.source == recovery.source
            and power_source.size >= recovery.bucket_count
        ):
            signature = _closed_signature(power_source, recovery.bucket_count)
            signature_size = recovery.bucket_count
        elif (
            previous is not None
            and previous.recovery.source == recovery.source
            and previous.closed_signature_size is not None
            and previous.closed_signature_size <= recovery.bucket_count
        ):
            signature = previous.closed_signature
            signature_size = previous.closed_signature_size
        checkpoint = _PowerCheckpoint(signature, signature_size, recovery)
        if checkpoint != previous or self._power_checkpoint_needs_migration:
            try:
                await self._power_store.async_save(checkpoint.as_dict())
            except (HomeAssistantError, OSError):
                _LOGGER.warning("Unable to save directional battery power checkpoint")
                return False
            self._saved_power_checkpoint = checkpoint
            self._power_checkpoint_needs_migration = False
        return await self._async_remove_embedded_checkpoint()

    async def _async_remove_embedded_checkpoint(self) -> bool:
        record = self._stored_history_record
        if self._store is None or record is None or "power" not in record:
            return True
        data = dict(record)
        del data["power"]
        try:
            await self._store.async_save(data)
        except (HomeAssistantError, OSError):
            _LOGGER.warning(
                "Unable to retire embedded directional battery power checkpoint"
            )
            return False
        self._stored_history_record = data
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
        ready = (
            self._power_loaded and self._history_readable if for_power else self._loaded
        )
        if not self._enabled or not ready or self._zone is None:
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
            if (
                previous is not None
                and previous.untrusted_power_pairs
                and (history.source, history.size) == (previous.source, previous.size)
            ):
                # The same day's cumulative counter still includes the
                # rejected advance until power independently reevaluates it.
                history = replace(
                    history,
                    untrusted_power_pairs=previous.untrusted_power_pairs,
                )
            self._history = history
        return {field: (total, count) for field, total in history.totals.items()}

    @property
    def power_source_context_ready(self) -> bool:
        # A transient read failure can still conceal a newer cumulative floor.
        # A successful read may establish that there is no usable floor.
        return (
            self._enabled
            and self._power_loaded
            and self._history_readable
            and self._zone is not None
        )

    @property
    def power_source(self) -> DirectionalBatterySource:
        history = self._power_source_history
        assert history is not None
        return history.source

    @property
    def power_source_signature(self) -> str:
        history = self._power_source_history
        assert history is not None
        return history.signature

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
                and checkpoint.closed_signature_size is not None
                and _closed_signature(history, checkpoint.closed_signature_size)
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
        recovery = (
            checkpoint.recovery
            if checkpoint is not None
            and checkpoint.recovery.source == current.source
            and checkpoint.recovery.bucket_count == current.size
            else None
        )
        saved = self._saved_history
        if saved is not None and (saved.source, saved.size) != (
            current.source,
            current.size,
        ):
            saved = None
        return _startup_recovery(recovery, saved)

    def power_startup_for_rejected_report(self) -> DirectionalBatteryRecovery | None:
        """Retain the site clock on the last trusted source checkpoint."""
        checkpoint = self._saved_power_checkpoint
        return _startup_recovery(
            checkpoint.recovery if checkpoint is not None else None,
            self._saved_history,
        )

    def cumulative_power_floor(self) -> DirectionalBatteryRecovery:
        """Return the latest cumulative history after its publication is acknowledged."""
        assert self._history is not None
        return _legacy_recovery(self._history)

    def mark_untrusted_power_pairs(
        self,
        observed: DirectionalBatteryRecovery | None,
        rejected: frozenset[str],
    ) -> None:
        """Record rejections with cumulative energy before either write can fail."""
        history = self._history
        assert history is not None
        if observed is None or history.signature not in observed.observed_signatures:
            self._history = replace(
                history, untrusted_power_pairs=history.untrusted_power_pairs | rejected
            )
            return
        cumulative = _legacy_recovery(history)
        untrusted = frozenset(
            aggregate
            for aggregate, fields in _AGGREGATE_PAIRS.items()
            if observed.pairs[aggregate].reported_at < history.reported_at
            and any(
                cumulative.pairs[aggregate].wh[field]
                > observed.pairs[aggregate].wh[field]
                for field in fields
            )
        )
        self._history = replace(history, untrusted_power_pairs=untrusted)

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
