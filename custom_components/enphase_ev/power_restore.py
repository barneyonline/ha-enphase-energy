"""Bounded accepted power history, independent of live calculation baselines."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from homeassistant.helpers.restore_state import ExtraStoredData

from .const import CURRENT_POWER_STALE_AFTER_S, PRODUCTION_POWER_IDLE_MAX_W
from .power_validation import EXTREME_SITE_POWER_W


def power_restore_data(extra: ExtraStoredData | None) -> dict[str, Any]:
    """Ignore malformed HA extra-data envelopes before parsing their contents."""
    raw = extra.as_dict() if extra is not None else None
    return raw if isinstance(raw, dict) else {}


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


@dataclass(frozen=True)
class PowerReading:
    """An accepted value with its original calculation and expiry clocks."""

    watts: float
    sampled_ts: float
    freshness_ts: float
    attributes: dict[str, Any]
    received_ts: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "watts": self.watts,
            "sampled_ts": self.sampled_ts,
            "freshness_ts": self.freshness_ts,
            "attributes": dict(self.attributes),
            "received_ts": self.received_ts,
        }

    @classmethod
    def from_dict(cls, raw: object, now_ts: float) -> PowerReading | None:
        if not isinstance(raw, dict):
            return None
        watts = _finite_number(raw.get("watts"))
        sampled = _finite_number(raw.get("sampled_ts"))
        freshness = _finite_number(raw.get("freshness_ts"))
        attributes = raw.get("attributes")
        if (
            watts is None
            or abs(watts) >= EXTREME_SITE_POWER_W
            or sampled is None
            or not 0 < sampled <= now_ts
            or freshness is None
            or not 0 < freshness <= now_ts
            or now_ts - freshness >= CURRENT_POWER_STALE_AFTER_S
            or not isinstance(attributes, dict)
        ):
            return None
        received = _finite_number(raw.get("received_ts"))
        if raw.get("received_ts") is not None and (
            received is None or not 0 < received <= now_ts
        ):
            return None
        idle = (
            received is not None
            and abs(watts) <= PRODUCTION_POWER_IDLE_MAX_W
            and sampled <= received
        )
        freshness_limit = min(sampled, received or sampled)
        if idle and received is not None:
            freshness_limit = received
        if freshness > freshness_limit:
            return None
        return cls(watts, sampled, freshness, dict(attributes), received)


class PowerHistory:
    """Retain only two distinct accepted readings; never renew them on restore."""

    def __init__(self) -> None:
        self.samples: list[PowerReading] = []

    def restore(self, raw: object, now_ts: float, *, nonnegative: bool = False) -> None:
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return
        samples = raw.get("samples")
        if not isinstance(samples, list):
            return
        for item in samples[-2:]:
            reading = PowerReading.from_dict(item, now_ts)
            if reading is not None and (not nonnegative or reading.watts >= 0):
                self.remember(reading)

    def remember(self, reading: PowerReading) -> None:
        # A successful unchanged poll is not a new measurement. An independently
        # acquired overnight zero may have a newer effective freshness clock.
        if (
            not math.isfinite(reading.watts)
            or abs(reading.watts) >= EXTREME_SITE_POWER_W
        ):
            return
        if self.samples and (
            reading.freshness_ts <= self.samples[-1].freshness_ts
            and reading.sampled_ts <= self.samples[-1].sampled_ts
        ):
            previous = self.samples[-1]
            if (
                reading.sampled_ts == previous.sampled_ts
                and reading.freshness_ts == previous.freshness_ts
                and reading.watts != previous.watts
            ):
                self.samples[-1] = reading
            return
        self.samples = [*self.samples, reading][-2:]

    def latest(self, now_ts: float) -> PowerReading | None:
        for reading in reversed(self.samples):
            if 0 <= now_ts - reading.freshness_ts < CURRENT_POWER_STALE_AFTER_S:
                return reading
        return None

    def as_dict(self) -> dict[str, Any]:
        return {"version": 1, "samples": [sample.as_dict() for sample in self.samples]}


@dataclass
class PowerStoredData(ExtraStoredData):  # type: ignore[misc]
    """Extend existing HA restore formats without losing legacy fields."""

    legacy: ExtraStoredData
    history: PowerHistory
    baseline: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {**self.legacy.as_dict(), "power_history": self.history.as_dict()}
        if self.baseline is not None:
            data["power_baseline"] = self.baseline
        return data
