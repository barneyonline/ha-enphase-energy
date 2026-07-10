"""Fetch and cache EVSE feature flags used to gate charger capabilities."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from .const import (
    EVSE_FEATURE_FLAGS_CACHE_TTL,
    EVSE_FEATURE_FLAGS_FAILURE_BACKOFF_S,
)
from .log_redaction import redact_text
from .payload_debug import debug_payload_shape, debug_render_summary, debug_sorted_keys
from .parsing_helpers import coerce_optional_bool

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class EvseFeatureFlagsSnapshot:
    """Typed view of EVSE feature-flag cache state on the coordinator."""

    payload: dict[str, object] | None
    site_feature_flags: dict[str, object]
    charger_feature_flags_by_serial: dict[str, dict[str, object]]
    charger_serial_count: int

    @classmethod
    def from_coordinator(cls, coord: EnphaseCoordinator) -> EvseFeatureFlagsSnapshot:
        site = getattr(coord, "_evse_site_feature_flags", None)
        by_serial = getattr(coord, "_evse_feature_flags_by_serial", None)
        payload = getattr(coord, "_evse_feature_flags_payload", None)
        raw_by_serial = by_serial if isinstance(by_serial, dict) else {}
        return cls(
            payload=dict(payload) if isinstance(payload, dict) else None,
            site_feature_flags=dict(site) if isinstance(site, dict) else {},
            charger_feature_flags_by_serial=dict(
                (str(k), dict(v))
                for k, v in raw_by_serial.items()
                if isinstance(v, dict)
            ),
            charger_serial_count=len(raw_by_serial),
        )


def evse_feature_flag_debug_summary(
    snapshot: EvseFeatureFlagsSnapshot,
) -> dict[str, object]:
    """Build the debug summary dict without reading coordinator private helpers."""

    charger_flag_keys: set[str] = set()
    for flags in snapshot.charger_feature_flags_by_serial.values():
        if not isinstance(flags, dict):
            continue
        charger_flag_keys.update(debug_sorted_keys(flags))
    payload = snapshot.payload
    meta = payload.get("meta") if isinstance(payload, dict) else None
    error = payload.get("error") if isinstance(payload, dict) else None
    return {
        "site_flag_keys": sorted(
            str(key) for key in snapshot.site_feature_flags.keys()
        ),
        "charger_count": snapshot.charger_serial_count,
        "charger_flag_keys": sorted(charger_flag_keys),
        "meta_keys": debug_sorted_keys(meta),
        "error_keys": debug_sorted_keys(error),
    }


class EvseFeatureFlagsRuntime:
    """Fetch, parse, and cache EVSE management feature flags for capability gating."""

    def __init__(self, coordinator: EnphaseCoordinator) -> None:
        self.coordinator = coordinator

    def debug_feature_flag_summary(self) -> dict[str, object]:
        """Return a sanitized summary of EVSE feature-flag discovery."""

        return evse_feature_flag_debug_summary(
            EvseFeatureFlagsSnapshot.from_coordinator(self.coordinator)
        )

    def _cached_state_present(self) -> bool:
        coord = self.coordinator
        return bool(
            getattr(coord, "_evse_feature_flags_payload", None) is not None
            or getattr(coord, "_evse_site_feature_flags", None)
            or getattr(coord, "_evse_feature_flags_by_serial", None)
        )

    def refresh_due(self, *, force: bool = False) -> bool:
        """Return True when feature flags should refresh or cached state should clear."""

        coord = self.coordinator
        now = time.monotonic()
        if not force and coord._evse_feature_flags_cache_until:
            if now < coord._evse_feature_flags_cache_until:
                return False
        fetcher = getattr(coord.client, "evse_feature_flags", None)
        if not callable(fetcher):
            return self._cached_state_present()
        return True

    def feature_flag(self, key: str, sn: str | None = None) -> object | None:
        """Return a parsed EVSE feature flag for the site or charger."""

        coord = self.coordinator
        key_text = str(key).strip()
        if not key_text:
            return None
        if sn:
            serial_flags = getattr(coord, "_evse_feature_flags_by_serial", {}) or {}
            raw = serial_flags.get(str(sn), {}).get(key_text)
            if raw is not None:
                return cast(object, raw)
        site_flags = getattr(coord, "_evse_site_feature_flags", {}) or {}
        return cast(object | None, site_flags.get(key_text))

    def feature_flag_enabled(self, key: str, sn: str | None = None) -> bool | None:
        """Return a feature flag coerced to a tri-state boolean."""

        return coerce_optional_bool(self.feature_flag(key, sn))

    @staticmethod
    def coerce_evse_feature_flags_map(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {}
        out: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            try:
                key = str(raw_key).strip()
            except Exception:
                continue
            if not key:
                continue
            out[key] = raw_value
        return out

    def parse_payload(self, payload: object) -> None:
        """Cache site and charger feature flags from the EVSE management payload."""

        coord = self.coordinator
        coord._evse_site_feature_flags = {}
        coord._evse_feature_flags_by_serial = {}
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        site_flags: dict[str, object] = {}
        charger_flags: dict[str, dict[str, object]] = {}
        for raw_key, raw_value in data.items():
            try:
                key = str(raw_key).strip()
            except Exception:
                continue
            if not key:
                continue
            if isinstance(raw_value, dict):
                flags = self.coerce_evse_feature_flags_map(raw_value)
                if flags:
                    charger_flags[key] = flags
                continue
            site_flags[key] = raw_value
        coord._evse_site_feature_flags = site_flags
        coord._evse_feature_flags_by_serial = charger_flags

    async def async_refresh(self, *, force: bool = False) -> None:
        """Refresh EVSE feature flags used for capability gating."""

        coord = self.coordinator
        now = time.monotonic()
        if not force and coord._evse_feature_flags_cache_until:
            if now < coord._evse_feature_flags_cache_until:
                return
        fetcher = getattr(coord.client, "evse_feature_flags", None)
        if not callable(fetcher):
            # Older client versions do not expose feature flags, so cached state
            # must clear.
            coord._evse_feature_flags_payload = None
            coord._evse_site_feature_flags = {}
            coord._evse_feature_flags_by_serial = {}
            return
        country = getattr(coord, "_battery_country_code", None)
        try:
            payload = await fetcher(country=country)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "EVSE feature flags fetch failed: %s",
                redact_text(err, site_ids=(coord.site_id,)),
            )
            # Failed flag discovery should not hammer the management endpoint.
            coord._evse_feature_flags_cache_until = (
                now + EVSE_FEATURE_FLAGS_FAILURE_BACKOFF_S
            )
            return
        if not isinstance(payload, dict):
            coord._evse_feature_flags_payload = None
            coord._evse_site_feature_flags = {}
            coord._evse_feature_flags_by_serial = {}
            coord._evse_feature_flags_cache_until = (
                now + EVSE_FEATURE_FLAGS_FAILURE_BACKOFF_S
            )
            _LOGGER.debug(
                "EVSE feature flags payload shape was invalid: %s",
                debug_render_summary(debug_payload_shape(payload)),
            )
            return
        coord._evse_feature_flags_payload = dict(payload)
        self.parse_payload(payload)
        coord._evse_feature_flags_cache_until = now + EVSE_FEATURE_FLAGS_CACHE_TTL
        coord._debug_log_summary_if_changed(
            "evse_feature_flags",
            "EVSE feature flag summary",
            self.debug_feature_flag_summary(),
        )
