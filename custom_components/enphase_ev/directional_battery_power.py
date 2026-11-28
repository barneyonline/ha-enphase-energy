"""Calculate one coherent power window for all four site battery directions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
import math

from .const import CLOUD_TELEMETRY_STALE_AFTER_S
from .directional_battery_energy import _AGGREGATE_PAIRS, _FIELDS, _pairs_reconcile
from .power_average import EnergyPowerAverage
from .power_validation import (
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

    reported_at: datetime
    bucket_count: int
    wh: dict[str, float]


class DirectionalBatteryPower:
    """Derive watts from fresh, independently reconciled source intervals."""

    def __init__(self, minimum_window_seconds: float) -> None:
        self._minimum_window_seconds = minimum_window_seconds
        self._baseline: _Report | None = None
        self._last_report: _Report | None = None
        self._recovery_reference: _Report | None = None
        self._dropout_watermarks: dict[str, dict[str, float]] = {}
        self._validators = {field: ExtremePowerValidator() for field in _FIELDS}
        self._averages = {field: EnergyPowerAverage() for field in _FIELDS}
        self.qualified = False
        self.interval: DirectionalBatteryPowerReading | None = None
        self.average: DirectionalBatteryPowerReading | None = None

    def invalidate(self) -> None:
        """Discard the window while retaining the source timestamp watermark."""
        self._baseline = None
        self.interval = self.average = None
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
        startup_wh: Mapping[str, float] | None = None,
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
                self.invalidate()
                return
            wh[field] = value
        self._observe(
            _Report(reported_at, bucket_count, wh),
            received_at,
            totals,
            startup_wh,
        )

    def _observe(
        self,
        report: _Report,
        received_at: datetime,
        totals: dict[str, tuple[float, int]],
        startup_wh: Mapping[str, float] | None,
    ) -> None:
        previous = self._last_report
        if previous is not None and report.reported_at <= previous.reported_at:
            if report != previous:
                self.invalidate()
            return
        self._last_report = report
        recovering = bool(self._dropout_watermarks)
        rollover = previous is not None and report.bucket_count != previous.bucket_count
        if rollover:
            self._dropout_watermarks.clear()
        else:
            self._track_dropouts(
                report, self._recovery_reference or previous, startup_wh
            )
        if previous is None or rollover or recovering or self._dropout_watermarks:
            self._recovery_reference = report
        if self._dropout_watermarks:
            self.invalidate()
            return
        baseline = self._baseline
        if (
            baseline is None
            or report.bucket_count != baseline.bucket_count
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
            self.invalidate()
            self._baseline = report
            return

        seconds = (report.reported_at - baseline.reported_at).total_seconds()
        if seconds < self._minimum_window_seconds:
            return
        deltas = {
            field: value - baseline.wh[field] for field, value in report.wh.items()
        }
        for total, (left, right) in _AGGREGATE_PAIRS.items():
            direct = deltas[total]
            pair = deltas[left] + deltas[right]
            if (direct == 0) != (pair == 0) or not _pairs_reconcile(direct, pair):
                self.invalidate()
                return

        if not all(math.isfinite(deltas[field] * 3600.0) for field in _FIELDS):
            self.invalidate()
            return
        self._baseline = report
        watts = {
            field: _energy_delta_to_power_w(deltas[field] / 1000.0, window_s=seconds)
            for field in _FIELDS
        }
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
            self.interval = self.average = None
            for average in self._averages.values():
                average.clear()
            return

        self.qualified = True
        self._recovery_reference = report
        self.interval = DirectionalBatteryPowerReading(
            report.reported_at, seconds, watts
        )
        if any(result.confirmed_extreme for result in validation.values()):
            for average in self._averages.values():
                average.clear()
            self.average = self.interval
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
        self.average = DirectionalBatteryPowerReading(
            report.reported_at, window, averaged_watts
        )

    def _track_dropouts(
        self,
        report: _Report,
        previous: _Report | None,
        startup_wh: Mapping[str, float] | None,
    ) -> None:
        """Require the entire affected pair to recover before reseeding."""
        for aggregate, fields in _AGGREGATE_PAIRS.items():
            if (watermarks := self._dropout_watermarks.get(aggregate)) is not None:
                if all(
                    report.wh[field] >= value for field, value in watermarks.items()
                ):
                    del self._dropout_watermarks[aggregate]
            elif previous is not None:
                if report.wh[aggregate] < previous.wh[aggregate] and any(
                    report.wh[field] < previous.wh[field] for field in fields
                ):
                    self._dropout_watermarks[aggregate] = {
                        field: previous.wh[field] for field in (aggregate, *fields)
                    }
            elif startup_wh is not None:
                saved = {
                    field: startup_wh[field]
                    for field in (aggregate, *fields)
                    if field in startup_wh
                }
                if any(report.wh[field] < value for field, value in saved.items()):
                    self._dropout_watermarks[aggregate] = saved

    def recovery_wh(self, reported_at: datetime) -> dict[str, float] | None:
        """Checkpoint raw counters and pre-drop values without saving watts."""
        report = self._last_report
        if report is None or report.reported_at != reported_at:
            return None
        # A revision or unconfirmed outlier can move the calculation baseline
        # before the next trustworthy report. Retain the earlier guard.
        reference = self._recovery_reference
        assert reference is not None
        wh = reference.wh.copy()
        for watermarks in self._dropout_watermarks.values():
            wh.update(watermarks)
        return wh
