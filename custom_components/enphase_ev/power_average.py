"""Bounded time-weighted averages of cumulative energy, without future data."""

from __future__ import annotations

import math
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, cast

from .power_validation import EXTREME_SITE_POWER_W, ExtremePowerValidator

WINDOW_SECONDS = 900.0
MAX_GAP_SECONDS = 1800.0


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class AverageUpdate:
    """Immutable presentation result from an accepted interval observation."""

    watts: int | None
    window_seconds: float | None
    method: str


class EnergyPowerAverage:
    """Interpolate only the left edge of an observed cumulative-energy window.

    Startup uses the available elapsed window, never zero padding or a larger
    divisor applied to an unaccumulated increment. Equal-clock corrections are
    deferred until the next advancing observation by the caller.
    """

    def __init__(self) -> None:
        self.samples: list[tuple[float, float]] = []
        self.clock: str | None = None
        self._latest: AverageUpdate | None = None
        self._accepted: AverageUpdate | None = None
        self._extreme_validator = ExtremePowerValidator()

    def clear(self) -> None:
        self.samples = []
        self.clock = None
        self._latest = None
        self._accepted = None
        self._extreme_validator.clear()

    def _result(self, update: AverageUpdate) -> AverageUpdate:
        self._latest = update
        return update

    def observe_lifetime(
        self,
        *,
        timestamp: float | None,
        values: Mapping[str, float],
        signs: Mapping[str, int],
        source_clock: bool,
        advanced: bool,
        interval: AverageUpdate,
    ) -> AverageUpdate:
        """Apply continuity policy to an already validated lifetime observation."""

        if not values or timestamp is None:
            self.clear()
            return self._result(AverageUpdate(None, None, interval.method))
        if not advanced:
            # A duplicate must retain quarantine too, rather than exposing the
            # differently scaled raw interval value on a second callback.
            return self._latest or self._result(
                AverageUpdate(None, None, interval.method)
            )
        reseeded = interval.method in {
            "lifetime_reset",
            "source_changed_reseed",
            "extreme_confirmed",
        }
        if reseeded or interval.method not in {
            "lifetime_energy_window",
            "no_change",
            "seeded",
        }:
            self.clear()
            if not reseeded:
                return self._result(interval)
        self.add(
            timestamp,
            sum(values.get(key, 0) * sign for key, sign in signs.items()),
            "source" if source_clock else "receipt",
        )
        if reseeded:
            # A reset/reseed or confirmed extreme is a valid new baseline. Its
            # first following increment belongs in the rolling energy window.
            return self._result(interval)
        average = self.value()
        if average is None:
            return self._result(AverageUpdate(None, None, interval.method))
        watts, seconds = average
        validation = self._extreme_validator.evaluate(
            watts, sample_ts=timestamp if source_clock else None
        )
        if not validation.accepted:
            return self._result(
                AverageUpdate(
                    self._accepted.watts if self._accepted else None,
                    self._accepted.window_seconds if self._accepted else None,
                    "extreme_pending",
                )
            )
        method = (
            "extreme_confirmed"
            if validation.confirmed_extreme
            else "no_change" if watts == 0 else "lifetime_energy_window"
        )
        self._accepted = AverageUpdate(watts, seconds, method)
        return self._result(self._accepted)

    def add_delta(
        self, baseline: float, timestamp: float, delta_kwh: float
    ) -> tuple[int, float]:
        """Accumulate a validated advancing consumption delta in the same clock."""

        if not self.samples or self.samples[-1][0] != baseline:
            self.clear()
            self.add(baseline, 0.0, "source")
        self.add(timestamp, self.samples[-1][1] + delta_kwh, "source")
        return cast(tuple[int, float], self.value())

    def add(self, timestamp: float, energy_kwh: float, clock: str) -> None:
        if not math.isfinite(timestamp) or not math.isfinite(energy_kwh):
            return
        if self.samples and timestamp <= self.samples[-1][0]:
            return
        if self.clock != clock or (
            self.samples and timestamp - self.samples[-1][0] > MAX_GAP_SECONDS
        ):
            self.clear()
        self.clock = clock
        self.samples.append((timestamp, energy_kwh))
        # Retain the bracketing observation, not just points inside the window.
        while (
            len(self.samples) > 2 and self.samples[1][0] <= timestamp - WINDOW_SECONDS
        ):
            self.samples.pop(0)
        # Bound pathological sub-second clocks without approximating coverage.
        if len(self.samples) > 1024:
            self.samples = self.samples[-1024:]

    def value(self) -> tuple[int, float] | None:
        if len(self.samples) < 2:
            return None
        end, energy = self.samples[-1]
        start, baseline = self.samples[0]
        left = max(start, end - WINDOW_SECONDS)
        if left > start:
            next_ts, next_energy = self.samples[1]
            baseline += (next_energy - baseline) * (left - start) / (next_ts - start)
        seconds = end - left
        watts = (energy - baseline) * 3_600_000 / seconds
        return (round(watts), seconds) if math.isfinite(watts) else None

    def as_dict(self) -> dict[str, Any]:
        return {"version": 1, "clock": self.clock, "samples": list(self.samples)}

    def restore(self, raw: object, now: float) -> None:
        self.clear()
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return
        clock, samples = raw.get("clock"), raw.get("samples")
        if (
            not isinstance(clock, str)
            or clock not in {"source", "receipt"}
            or not isinstance(samples, list)
        ):
            return
        for item in samples[-1024:]:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                self.clear()
                return
            stamp, energy = (_finite_float(value) for value in item)
            if (
                stamp is None
                or energy is None
                or not 0 < stamp <= now
                or (self.samples and stamp <= self.samples[-1][0])
            ):
                self.clear()
                return
            self.add(float(stamp), float(energy), clock)
        if self.samples and now - self.samples[-1][0] >= MAX_GAP_SECONDS:
            self.clear()
        average = self.value()
        if len(self.samples) >= 2 and (
            average is None or abs(average[0]) >= EXTREME_SITE_POWER_W
        ):
            self.clear()
            return
        if average is not None:
            watts, seconds = average
            self._accepted = AverageUpdate(
                watts,
                seconds,
                (
                    "restored_no_change"
                    if watts == 0
                    else "restored_lifetime_energy_window"
                ),
            )
            self._latest = self._accepted
