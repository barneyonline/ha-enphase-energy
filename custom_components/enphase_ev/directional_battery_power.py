"""Calculate one coherent power window for all four site battery directions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
import math

from .const import CLOUD_TELEMETRY_STALE_AFTER_S
from .directional_battery_energy import (
    _AGGREGATE_PAIRS,
    _FIELDS,
    _pairs_reconcile,
    DirectionalBatteryPairReference,
    DirectionalBatteryRecovery,
    DirectionalBatterySource,
)
from .power_average import EnergyPowerAverage
from .power_validation import (
    EXTREME_SITE_POWER_W,
    ExtremePowerValidator,
    _energy_delta_to_power_w,
    site_power_sample_is_plausible,
)

# The lifetime recordings available for #1029 establish daily coverage.
# See docs/api/directional_battery_power.md for the outstanding window evidence.
DIRECTIONAL_BATTERY_POWER_SOURCE_VALIDATED = False

DIRECTIONAL_BATTERY_POWER_FIELDS = {
    "battery_power_from_grid": "grid_battery",
    "battery_power_from_solar": "solar_battery",
    "battery_power_to_home": "battery_home",
    "battery_power_to_grid": "battery_grid",
}


@dataclass(frozen=True, slots=True)
class DirectionalBatteryPowerReading:
    """Four readings whose source and calculation windows agree."""

    reported_at: datetime
    window_seconds: float
    watts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _Report:
    """The current partial day's raw Wh counters and report clock."""

    source: DirectionalBatterySource
    reported_at: datetime
    bucket_count: int
    wh: dict[str, float]


class DirectionalBatteryPower:
    """Derive watts from fresh, independently reconciled source intervals."""

    def __init__(self, minimum_window_seconds: float) -> None:
        self._minimum_window_seconds = minimum_window_seconds
        self._baseline: _Report | None = None
        self._last_report: _Report | None = None
        self._source_cursor: datetime | None = None
        self._recovery_scope: tuple[DirectionalBatterySource, int] | None = None
        self._recovery_references: dict[str, DirectionalBatteryPairReference] = {}
        self._validators = {field: ExtremePowerValidator() for field in _FIELDS}
        self._averages = {field: EnergyPowerAverage() for field in _FIELDS}
        self._next_interval: DirectionalBatteryPowerReading | None = None
        self._next_average: DirectionalBatteryPowerReading | None = None
        self.qualified = False
        self.interval: DirectionalBatteryPowerReading | None = None
        self.average: DirectionalBatteryPowerReading | None = None

    def invalidate(self) -> None:
        """Discard the window while retaining the source timestamp watermark."""
        self._reset_window()
        self.publish()

    def publish(self) -> None:
        """Expose the staged window after the source checkpoint is acknowledged."""
        self.interval = self._next_interval
        self.average = self._next_average
        self.qualified |= self.interval is not None

    def source_clock_is_current(self, reported_at: datetime) -> bool:
        """Check the site clock before validating or updating source history."""
        return (
            self._source_cursor is None
            or reported_at > self._source_cursor
            or (
                reported_at == self._source_cursor
                and self._last_report is not None
                and self._last_report.reported_at == reported_at
            )
        )

    def reject_report(
        self, reported_at: datetime, startup: DirectionalBatteryRecovery | None = None
    ) -> None:
        """Retain the first unavailable interval without trusting its counters."""
        self._reset_window()
        if self._recovery_scope is None and startup is not None:
            self._restore_recovery(startup)
        if self._source_cursor is not None and reported_at <= self._source_cursor:
            return
        self._source_cursor = reported_at
        self._limit_recovery_elapsed(reported_at)

    def _restore_recovery(self, startup: DirectionalBatteryRecovery) -> None:
        self._recovery_references = startup.pairs.copy()
        self._recovery_scope = startup.source, startup.bucket_count
        if self._source_cursor is None or self._source_cursor <= startup.reported_at:
            self._source_cursor = startup.reported_at
        else:
            self._limit_recovery_elapsed(self._source_cursor)

    def _limit_recovery_elapsed(self, reported_at: datetime) -> None:
        for aggregate, reference in self._recovery_references.items():
            elapsed = (reported_at - reference.reported_at).total_seconds()
            self._recovery_references[aggregate] = replace(
                reference,
                max_elapsed_seconds=_allowed_elapsed(
                    elapsed, reference.max_elapsed_seconds, self._minimum_window_seconds
                ),
            )

    def _reset_window(self) -> None:
        self._baseline = None
        self._next_interval = self._next_average = None
        for field in _FIELDS:
            self._validators[field].clear()
            self._averages[field].clear()

    def observe(
        self,
        payload: dict[str, object],
        reported_at: datetime,
        received_at: datetime,
        totals: dict[str, tuple[float, int]],
        coerce_bucket: Callable[[object], float | None],
        *,
        source: DirectionalBatterySource,
        startup: DirectionalBatteryRecovery | None = None,
    ) -> None:
        """Use a daily payload accepted for power by the lifetime source validator."""
        wh: dict[str, float] = {}
        directional_buckets = payload[_FIELDS[0]]
        assert isinstance(directional_buckets, list)
        bucket_count = len(directional_buckets)
        # Direct aggregates can contain trailing nulls beyond the current day.
        for field in (*_FIELDS, *_AGGREGATE_PAIRS):
            buckets = payload.get(field)
            if (
                not isinstance(buckets, list)
                or len(buckets) < bucket_count
                or isinstance(raw := buckets[bucket_count - 1], bool)
                or (value := coerce_bucket(raw)) is None
            ):
                self.reject_report(reported_at, startup)
                return
            wh[field] = value
        self._observe(
            _Report(source, reported_at, bucket_count, wh),
            received_at,
            totals,
            startup,
        )

    def _observe(
        self,
        report: _Report,
        received_at: datetime,
        totals: dict[str, tuple[float, int]],
        startup: DirectionalBatteryRecovery | None,
    ) -> None:
        previous = self._last_report
        if self._recovery_scope is None and startup is not None:
            self._restore_recovery(startup)
        if (
            self._source_cursor is not None
            and report.reported_at <= self._source_cursor
        ):
            if report != previous:
                self._reset_window()
            return
        self._source_cursor = report.reported_at
        self._last_report = report
        scope = report.source, report.bucket_count
        if self._recovery_scope is not None and scope != self._recovery_scope:
            self._recovery_references.clear()
            self._reset_window()
        self._recovery_scope = scope
        self._track_recovery(report, totals)
        if any(reference.held for reference in self._recovery_references.values()):
            self._reset_window()
            return
        baseline = self._baseline
        if (
            baseline is None
            or (report.reported_at - baseline.reported_at).total_seconds()
            >= CLOUD_TELEMETRY_STALE_AFTER_S
            or (received_at - baseline.reported_at).total_seconds()
            >= CLOUD_TELEMETRY_STALE_AFTER_S
            or (
                previous is not None
                and any(
                    value < previous.wh[field] for field, value in report.wh.items()
                )
            )
        ):
            self._reset_window()
            self._baseline = report
            return

        seconds = (report.reported_at - baseline.reported_at).total_seconds()
        if seconds < self._minimum_window_seconds:
            return
        deltas = {
            field: value - baseline.wh[field] for field, value in report.wh.items()
        }
        watts: dict[str, int] = {}
        for aggregate, fields in _AGGREGATE_PAIRS.items():
            pair_watts = _pair_watts(deltas, seconds, aggregate, fields)
            if pair_watts is None:
                self._reset_window()
                return
            watts.update(pair_watts)
        self._baseline = report
        validation = {
            field: validator.evaluate(
                watts[field], sample_ts=report.reported_at.timestamp()
            )
            for field, validator in self._validators.items()
        }
        if any(
            not result.accepted
            or (
                not result.confirmed_extreme
                and not site_power_sample_is_plausible(
                    watts[field], deltas[field] / 1000.0, (totals[field][0] / 1000.0,)
                )
            )
            for field, result in validation.items()
        ):
            self._next_interval = self._next_average = None
            for average in self._averages.values():
                average.clear()
            return

        for aggregate, fields in _AGGREGATE_PAIRS.items():
            trusted = self._recovery_references[aggregate]
            confirmed = {
                field for field in fields if validation[field].confirmed_extreme
            }
            if confirmed or any(
                report.wh[field] < value for field, value in trusted.wh.items()
            ):
                self._recovery_references[aggregate] = _confirm_pair_revision(
                    trusted, report, totals, aggregate, fields, confirmed
                )
        self._next_interval = DirectionalBatteryPowerReading(
            report.reported_at, seconds, watts
        )
        if any(result.confirmed_extreme for result in validation.values()):
            for average in self._averages.values():
                average.clear()
            self._next_average = self._next_interval
            return
        averaged_watts: dict[str, int] = {}
        window = seconds
        for field, average in self._averages.items():
            if not average.samples:
                average.add(
                    baseline.reported_at.timestamp(),
                    baseline.wh[field] / 1000.0,
                    "source",
                )
            average.add(
                report.reported_at.timestamp(), report.wh[field] / 1000.0, "source"
            )
            value = average.value()
            assert value is not None
            averaged_watts[field], window = value
        self._next_average = DirectionalBatteryPowerReading(
            report.reported_at, window, averaged_watts
        )

    def _track_recovery(
        self,
        report: _Report,
        totals: dict[str, tuple[float, int]],
    ) -> None:
        for aggregate, fields in _AGGREGATE_PAIRS.items():
            reference = self._recovery_references.get(aggregate)
            if reference is None:
                reference = _pair_reference(report, aggregate, fields)
            else:
                reference = _advance_pair_reference(
                    reference,
                    report,
                    totals,
                    aggregate,
                    fields,
                    self._minimum_window_seconds,
                )
            self._recovery_references[aggregate] = reference

    def recovery_checkpoint(
        self, reported_at: datetime
    ) -> DirectionalBatteryRecovery | None:
        """Checkpoint the raw source cursor and recovery state without watts."""
        if self._recovery_scope is None or reported_at != self._source_cursor:
            return None
        source, bucket_count = self._recovery_scope
        return DirectionalBatteryRecovery(
            source, reported_at, bucket_count, self._recovery_references.copy()
        )


def _pair_reference(
    report: _Report, aggregate: str, fields: tuple[str, str]
) -> DirectionalBatteryPairReference:
    return DirectionalBatteryPairReference(
        report.reported_at,
        {field: report.wh[field] for field in (aggregate, *fields)},
    )


def _advance_pair_reference(
    reference: DirectionalBatteryPairReference,
    report: _Report,
    totals: dict[str, tuple[float, int]],
    aggregate: str,
    fields: tuple[str, str],
    minimum_window_seconds: float,
) -> DirectionalBatteryPairReference:
    deltas = {field: report.wh[field] - value for field, value in reference.wh.items()}
    held = reference.held or (
        any(deltas[field] < 0 for field in fields)
        and (aggregate not in deltas or deltas[aggregate] < 0)
    )
    elapsed = (report.reported_at - reference.reported_at).total_seconds()
    allowed_elapsed = _allowed_elapsed(
        elapsed, reference.max_elapsed_seconds, minimum_window_seconds
    )
    if not _counter_advances_are_plausible(deltas, allowed_elapsed, fields, totals):
        # The directional jump is visible even while the direct aggregate
        # lags. Retain this time allowance until the jump is resolved.
        return replace(reference, held=held, max_elapsed_seconds=allowed_elapsed)
    if (
        any(delta > 0 for delta in deltas.values())
        and aggregate in deltas
        and not _pair_deltas_reconcile(deltas, aggregate, fields)
    ):
        # Some counters have advanced and their partners have yet to agree.
        # Keep the first observed interval when evaluating a later catch-up.
        return replace(reference, held=held, max_elapsed_seconds=allowed_elapsed)
    if any(delta < 0 for delta in deltas.values()):
        return replace(reference, held=held)
    return _pair_reference(report, aggregate, fields)


def _allowed_elapsed(elapsed: float, maximum: float | None, minimum: float) -> float:
    return max(minimum, elapsed if maximum is None else min(elapsed, maximum))


def _confirm_pair_revision(
    reference: DirectionalBatteryPairReference,
    report: _Report,
    totals: dict[str, tuple[float, int]],
    aggregate: str,
    fields: tuple[str, str],
    confirmed: set[str],
) -> DirectionalBatteryPairReference:
    elapsed = reference.max_elapsed_seconds
    if elapsed is not None:
        deltas = {
            field: 0.0 if field in confirmed else report.wh[field] - reference.wh[field]
            for field in fields
        }
        if not _counter_advances_are_plausible(deltas, elapsed, fields, totals):
            return reference
    return _pair_reference(report, aggregate, fields)


def _pair_watts(
    deltas: Mapping[str, float],
    seconds: float,
    aggregate: str,
    fields: tuple[str, str],
) -> dict[str, int] | None:
    if not _pair_deltas_reconcile(deltas, aggregate, fields):
        return None
    return _direction_watts(deltas, seconds, fields)


def _pair_deltas_reconcile(
    deltas: Mapping[str, float], aggregate: str, fields: tuple[str, str]
) -> bool:
    direct = deltas[aggregate]
    paired = sum(deltas[field] for field in fields)
    return (
        direct >= 0
        and (direct == 0) == (paired == 0)
        and _pairs_reconcile(direct, paired)
    )


def _direction_watts(
    deltas: Mapping[str, float], seconds: float, fields: tuple[str, str]
) -> dict[str, int] | None:
    if not all(
        deltas[field] >= 0 and math.isfinite(deltas[field] * 3600.0 / seconds)
        for field in fields
    ):
        return None
    return {
        field: _energy_delta_to_power_w(deltas[field] / 1000.0, window_s=seconds)
        for field in fields
    }


def _counter_advances_are_plausible(
    deltas: Mapping[str, float],
    elapsed: float,
    fields: tuple[str, str],
    totals: dict[str, tuple[float, int]],
) -> bool:
    advances = {field: max(0.0, deltas[field]) for field in fields}
    watts = _direction_watts(advances, elapsed, fields)
    return watts is not None and all(
        value < EXTREME_SITE_POWER_W
        and site_power_sample_is_plausible(
            value, advances[field] / 1000.0, (totals[field][0] / 1000.0,)
        )
        for field, value in watts.items()
    )
