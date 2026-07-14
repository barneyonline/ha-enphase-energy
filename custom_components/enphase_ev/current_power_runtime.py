"""Fetch and normalize Enphase current-power samples."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as _tz
from typing import TYPE_CHECKING

from .api import InvalidPayloadError
from .log_redaction import redact_site_id, redact_text
from .power_validation import ExtremePowerValidator

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)
CURRENT_POWER_CACHE_TTL_S = 60.0
CURRENT_POWER_ENDPOINT_FAMILY = "current_power"


@dataclass(frozen=True, slots=True)
class CurrentPowerSample:
    """Immutable site current-power snapshot owned by the runtime."""

    w: float | None = None
    sample_utc: datetime | None = None
    reported_units: str | None = None
    reported_precision: int | None = None
    source: str | None = None


class CurrentPowerRuntime:
    """Fetch and cache site current power consumption from the app API."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self._sample = CurrentPowerSample()
        self._cache_until_mono: float | None = None
        self.using_stale = False
        self._extreme_validator = ExtremePowerValidator()
        self._last_observed_value: float | None = None
        self._last_observed_units: str | None = None
        self._last_normalized_value_w: float | None = None
        self._last_observed_sample_utc: datetime | None = None
        self._validation_state = "unavailable"
        self._validation_reason: str | None = None

    @property
    def snapshot(self) -> CurrentPowerSample:
        """Return the current immutable power sample."""

        return self._sample

    def replace_snapshot(self, **changes: object) -> None:
        """Update one or more fields in the runtime-owned sample."""

        values: dict[str, object] = {
            "w": self._sample.w,
            "sample_utc": self._sample.sample_utc,
            "reported_units": self._sample.reported_units,
            "reported_precision": self._sample.reported_precision,
            "source": self._sample.source,
        }
        values.update(changes)
        self._sample = CurrentPowerSample(
            w=values["w"] if isinstance(values["w"], (float, int)) else None,
            sample_utc=(
                values["sample_utc"]
                if isinstance(values["sample_utc"], datetime)
                else None
            ),
            reported_units=(
                values["reported_units"]
                if isinstance(values["reported_units"], str)
                else None
            ),
            reported_precision=(
                values["reported_precision"]
                if isinstance(values["reported_precision"], int)
                else None
            ),
            source=values["source"] if isinstance(values["source"], str) else None,
        )

    def clear(self) -> None:
        """Reset cached current power consumption samples."""

        self._cache_until_mono = None
        self.using_stale = False
        self._extreme_validator.clear()
        self._last_observed_value = None
        self._last_observed_units = None
        self._last_normalized_value_w = None
        self._last_observed_sample_utc = None
        self._validation_state = "unavailable"
        self._validation_reason = None
        self._sample = CurrentPowerSample()

    @staticmethod
    def _parse_sample_utc(sample_time: object) -> datetime | None:
        if sample_time is None:
            return None
        try:
            sample_seconds = float(str(sample_time))
            if sample_seconds > 10**12:
                # The app API has returned both seconds and milliseconds
                # for this field across deployments.
                sample_seconds /= 1000.0
            return datetime.fromtimestamp(sample_seconds, tz=_tz.utc)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _normalize_units(units: object) -> tuple[str | None, float | None]:
        if units is None:
            return None, 1.0
        try:
            units_text = str(units).strip()
        except Exception:  # noqa: BLE001
            return None, None
        if not units_text:
            return None, 1.0
        if units_text in {"W", "w"}:
            return units_text, 1.0
        if units_text in {"kW", "kw", "KW"}:
            return units_text, 1000.0
        if units_text in {"mW", "mw"}:
            return units_text, 0.001
        return units_text, None

    def diagnostics(self) -> dict[str, object]:
        """Return a sanitized summary of current-power validation state."""

        accepted = self._sample
        accepted_sample = accepted.sample_utc
        return {
            "accepted_value_w": accepted.w,
            "accepted_sample_utc": (
                accepted_sample.isoformat()
                if isinstance(accepted_sample, datetime)
                else None
            ),
            "accepted_reported_units": accepted.reported_units,
            "accepted_reported_precision": accepted.reported_precision,
            "accepted_source": accepted.source,
            "last_observed_value": self._last_observed_value,
            "last_observed_units": self._last_observed_units,
            "last_normalized_value_w": self._last_normalized_value_w,
            "last_observed_sample_utc": (
                self._last_observed_sample_utc.isoformat()
                if self._last_observed_sample_utc is not None
                else None
            ),
            "validation_state": self._validation_state,
            "validation_reason": self._validation_reason,
            "pending_extreme_count": self._extreme_validator.pending_count,
            "using_stale": self.using_stale,
        }

    def _cached_state_present(self) -> bool:
        return self._sample != CurrentPowerSample()

    def refresh_due(self) -> bool:
        """Return True when current-power data can be refreshed."""

        fetcher = getattr(self.coordinator.client, "latest_power", None)
        if callable(fetcher):
            if not self.coordinator._endpoint_family_should_run(
                CURRENT_POWER_ENDPOINT_FAMILY
            ):
                return False
            cache_until = self._cache_until_mono
            if cache_until is not None and time.monotonic() < cache_until:
                return False
            return True
        return self._cached_state_present()

    async def async_refresh(self) -> None:
        """Refresh cached current power consumption from ``client.latest_power``."""

        coord = self.coordinator
        fetcher = getattr(coord.client, "latest_power", None)
        if not callable(fetcher):
            self.clear()
            return
        now = time.monotonic()
        cache_until = self._cache_until_mono
        if cache_until is not None and now < cache_until:
            return
        if not coord._endpoint_family_should_run(CURRENT_POWER_ENDPOINT_FAMILY):
            self.using_stale = self._cached_state_present()
            return

        try:
            payload = await fetcher()
        except Exception as err:  # noqa: BLE001
            coord._note_endpoint_family_failure(CURRENT_POWER_ENDPOINT_FAMILY, err)
            self.using_stale = self._cached_state_present()
            _LOGGER.debug(
                "Skipping current power consumption refresh for site %s: %s",
                redact_site_id(coord.site_id),
                redact_text(err, site_ids=(coord.site_id,)),
            )
            return

        if not isinstance(payload, dict):
            self._validation_state = "invalid_payload"
            self._validation_reason = "response_not_object"
            self._note_invalid_payload("Current-power response is not an object")
            return

        value = payload.get("value")
        try:
            numeric = float(str(value))
        except Exception:  # noqa: BLE001
            self._validation_state = "invalid_payload"
            self._validation_reason = "value_not_numeric"
            self._note_invalid_payload("Current-power response has no numeric value")
            return
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            self._validation_state = "invalid_payload"
            self._validation_reason = "value_not_finite"
            self._note_invalid_payload("Current-power response value is not finite")
            return

        sampled_at = self._parse_sample_utc(payload.get("time"))
        units, multiplier = self._normalize_units(payload.get("units"))
        self._last_observed_value = numeric
        self._last_observed_units = units
        self._last_observed_sample_utc = sampled_at
        if multiplier is None:
            self._last_normalized_value_w = None
            self._validation_state = "invalid_unit"
            self._validation_reason = "unsupported_power_unit"
            self._note_invalid_payload("Current-power response unit is unsupported")
            return
        normalized_w = numeric * multiplier
        self._last_normalized_value_w = normalized_w

        precision_raw = payload.get("precision")
        precision = None
        if precision_raw is not None:
            try:
                precision = int(precision_raw)
            except Exception:  # noqa: BLE001
                precision = None

        validation = self._extreme_validator.evaluate(
            normalized_w,
            sample_ts=(sampled_at.timestamp() if sampled_at is not None else None),
        )
        self._validation_state = validation.state
        self._validation_reason = validation.reason
        if not validation.accepted:
            self._cache_until_mono = now + CURRENT_POWER_CACHE_TTL_S
            self.using_stale = self._cached_state_present()
            coord._note_endpoint_family_success(CURRENT_POWER_ENDPOINT_FAMILY)
            return

        self._sample = CurrentPowerSample(
            w=normalized_w,
            sample_utc=sampled_at,
            reported_units=units,
            reported_precision=precision,
            source="app-api:get_latest_power",
        )
        self._cache_until_mono = now + CURRENT_POWER_CACHE_TTL_S
        self.using_stale = False
        coord._note_endpoint_family_success(CURRENT_POWER_ENDPOINT_FAMILY)

    def _note_invalid_payload(self, summary: str) -> None:
        """Back off malformed responses while retaining the last valid sample."""

        self._extreme_validator.clear()
        error = InvalidPayloadError(
            summary,
            endpoint="get_latest_power",
            failure_kind="invalid_current_power_payload",
        )
        self.coordinator._note_endpoint_family_failure(
            CURRENT_POWER_ENDPOINT_FAMILY, error
        )
        self.using_stale = self._cached_state_present()
