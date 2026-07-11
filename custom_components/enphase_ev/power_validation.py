"""Shared validation helpers for extreme site-power samples."""

from __future__ import annotations

from dataclasses import dataclass
import math

EXTREME_SITE_POWER_W = 1_000_000.0
EXTREME_CONFIRM_MIN_RATIO = 0.5
EXTREME_CONFIRM_MAX_RATIO = 2.0


@dataclass(frozen=True, slots=True)
class ExtremePowerValidationResult:
    """Describe whether an observed power sample can be published."""

    accepted: bool
    confirmed_extreme: bool
    state: str
    reason: str | None


class ExtremePowerValidator:
    """Quarantine extreme power until a newer comparable sample confirms it."""

    def __init__(self) -> None:
        self._pending_value_w: float | None = None
        self._pending_sample_ts: float | None = None

    @property
    def pending_count(self) -> int:
        """Return the number of samples currently held for confirmation."""

        return int(self._pending_value_w is not None)

    @property
    def pending_value_w(self) -> float | None:
        """Return the extreme value currently awaiting confirmation."""

        return self._pending_value_w

    @property
    def pending_sample_ts(self) -> float | None:
        """Return the source timestamp for the pending extreme value."""

        return self._pending_sample_ts

    def clear(self) -> None:
        """Clear any unconfirmed extreme sample."""

        self._pending_value_w = None
        self._pending_sample_ts = None

    @staticmethod
    def _same_sign(first: float, second: float) -> bool:
        return math.copysign(1.0, first) == math.copysign(1.0, second)

    @staticmethod
    def _comparable_magnitude(first: float, second: float) -> bool:
        ratio = abs(second) / abs(first)
        return EXTREME_CONFIRM_MIN_RATIO <= ratio <= EXTREME_CONFIRM_MAX_RATIO

    def evaluate(
        self, value_w: float, *, sample_ts: float | None
    ) -> ExtremePowerValidationResult:
        """Return validation state for one normalized power sample."""

        if abs(value_w) < EXTREME_SITE_POWER_W:
            self.clear()
            return ExtremePowerValidationResult(True, False, "accepted", None)

        pending_value = self._pending_value_w
        pending_ts = self._pending_sample_ts
        if pending_value is None:
            self._pending_value_w = value_w
            self._pending_sample_ts = sample_ts
            reason = (
                "extreme_sample_missing_timestamp"
                if sample_ts is None
                else "extreme_sample_requires_confirmation"
            )
            return ExtremePowerValidationResult(False, False, "pending_extreme", reason)

        timestamp_advanced = (
            sample_ts is not None and pending_ts is not None and sample_ts > pending_ts
        )
        if (
            timestamp_advanced
            and self._same_sign(pending_value, value_w)
            and self._comparable_magnitude(pending_value, value_w)
        ):
            self.clear()
            return ExtremePowerValidationResult(True, True, "confirmed_extreme", None)

        if sample_ts is None:
            reason = "extreme_sample_missing_timestamp"
        elif pending_ts is None or sample_ts <= pending_ts:
            reason = "extreme_sample_timestamp_not_newer"
        else:
            reason = "extreme_sample_not_comparable"
            self._pending_value_w = value_w
            self._pending_sample_ts = sample_ts
        return ExtremePowerValidationResult(False, False, "pending_extreme", reason)
