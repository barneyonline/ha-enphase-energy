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

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)
CURRENT_POWER_CACHE_TTL_S = 60.0
CURRENT_POWER_ENDPOINT_FAMILY = "current_power"


@dataclass(slots=True)
class CurrentPowerSample:
    """Typed snapshot of site current-power fields mirrored on the coordinator."""

    w: float | None = None
    sample_utc: datetime | None = None
    reported_units: str | None = None
    reported_precision: int | None = None
    source: str | None = None

    def apply_to(self, coord: EnphaseCoordinator) -> None:
        coord._current_power_consumption_w = self.w
        coord._current_power_consumption_sample_utc = self.sample_utc
        coord._current_power_consumption_reported_units = self.reported_units
        coord._current_power_consumption_reported_precision = self.reported_precision
        coord._current_power_consumption_source = self.source


class CurrentPowerRuntime:
    """Fetch and cache site current power consumption from the app API."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator
        self._cache_until_mono: float | None = None
        self.using_stale = False

    def clear(self) -> None:
        """Reset cached current power consumption samples."""

        self._cache_until_mono = None
        self.using_stale = False
        CurrentPowerSample().apply_to(self.coordinator)

    def _cached_state_present(self) -> bool:
        coord = self.coordinator
        return any(
            getattr(coord, attr, None) is not None
            for attr in (
                "_current_power_consumption_w",
                "_current_power_consumption_sample_utc",
                "_current_power_consumption_reported_units",
                "_current_power_consumption_reported_precision",
                "_current_power_consumption_source",
            )
        )

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
            self._note_invalid_payload("Current-power response is not an object")
            return

        value = payload.get("value")
        try:
            numeric = float(str(value))
        except Exception:  # noqa: BLE001
            self._note_invalid_payload("Current-power response has no numeric value")
            return
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            self._note_invalid_payload("Current-power response value is not finite")
            return

        sampled_at = None
        sample_time = payload.get("time")
        if sample_time is not None:
            try:
                sample_seconds = float(str(sample_time))
                if sample_seconds > 10**12:
                    # The app API has returned both seconds and milliseconds
                    # for this field across deployments.
                    sample_seconds /= 1000.0
                sampled_at = datetime.fromtimestamp(sample_seconds, tz=_tz.utc)
            except Exception:  # noqa: BLE001
                sampled_at = None

        units = payload.get("units")
        if units is not None:
            try:
                units = str(units).strip()
            except Exception:  # noqa: BLE001
                units = None
            if not units:
                units = None

        precision_raw = payload.get("precision")
        precision = None
        if precision_raw is not None:
            try:
                precision = int(precision_raw)
            except Exception:  # noqa: BLE001
                precision = None

        CurrentPowerSample(
            w=numeric,
            sample_utc=sampled_at,
            reported_units=units,
            reported_precision=precision,
            source="app-api:get_latest_power",
        ).apply_to(coord)
        self._cache_until_mono = now + CURRENT_POWER_CACHE_TTL_S
        self.using_stale = False
        coord._note_endpoint_family_success(CURRENT_POWER_ENDPOINT_FAMILY)

    def _note_invalid_payload(self, summary: str) -> None:
        """Back off malformed responses while retaining the last valid sample."""

        error = InvalidPayloadError(
            summary,
            endpoint="get_latest_power",
            failure_kind="invalid_current_power_payload",
        )
        self.coordinator._note_endpoint_family_failure(
            CURRENT_POWER_ENDPOINT_FAMILY, error
        )
        self.using_stale = self._cached_state_present()
