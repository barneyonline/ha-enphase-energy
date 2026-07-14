"""Fetch and cache EVSE feature flags used to gate charger capabilities."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, cast

from .const import (
    EVSE_FEATURE_FLAGS_CACHE_TTL,
    EVSE_FEATURE_FLAGS_FAILURE_BACKOFF_S,
)
from .log_redaction import redact_text
from .payload_debug import debug_payload_shape, debug_render_summary, debug_sorted_keys
from .parsing_helpers import coerce_optional_bool
from .snapshot_helpers import freeze_snapshot_mapping

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EvseFeatureFlagsSnapshot:
    """Immutable view of EVSE feature-flag state owned by the runtime."""

    payload: Mapping[str, object] | None
    site_feature_flags: Mapping[str, object]
    charger_feature_flags_by_serial: Mapping[str, Mapping[str, object]]
    charger_serial_count: int

    @classmethod
    def from_coordinator(cls, coord: EnphaseCoordinator) -> EvseFeatureFlagsSnapshot:
        runtime = getattr(coord, "evse_feature_flags_runtime", None)
        if isinstance(runtime, EvseFeatureFlagsRuntime):
            return runtime.snapshot
        site = getattr(coord, "_evse_site_feature_flags", None)
        by_serial = getattr(coord, "_evse_feature_flags_by_serial", None)
        payload = getattr(coord, "_evse_feature_flags_payload", None)
        raw_by_serial = by_serial if isinstance(by_serial, dict) else {}
        return cls(
            payload=(
                freeze_snapshot_mapping(payload) if isinstance(payload, dict) else None
            ),
            site_feature_flags=freeze_snapshot_mapping(
                site if isinstance(site, dict) else {}
            ),
            charger_feature_flags_by_serial=cast(
                Mapping[str, Mapping[str, object]],
                freeze_snapshot_mapping(
                    {
                        str(k): freeze_snapshot_mapping(v)
                        for k, v in raw_by_serial.items()
                        if isinstance(v, dict)
                    }
                ),
            ),
            charger_serial_count=len(raw_by_serial),
        )


def evse_feature_flag_debug_summary(
    snapshot: EvseFeatureFlagsSnapshot,
) -> dict[str, object]:
    """Build the debug summary dict without reading coordinator private helpers."""

    charger_flag_keys: set[str] = set()
    for flags in snapshot.charger_feature_flags_by_serial.values():
        if not isinstance(flags, Mapping):
            continue
        charger_flag_keys.update(debug_sorted_keys(dict(flags)))
    payload = snapshot.payload
    meta = payload.get("meta") if isinstance(payload, Mapping) else None
    error = payload.get("error") if isinstance(payload, Mapping) else None
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
        self._cache_until_mono: float | None = None
        self._payload: dict[str, object] | None = None
        self._site_feature_flags: dict[str, object] = {}
        self._charger_feature_flags_by_serial: dict[str, object] = {}

    @property
    def snapshot(self) -> EvseFeatureFlagsSnapshot:
        """Return an immutable copy of the current feature-flag state."""

        raw_by_serial = self._charger_feature_flags_by_serial
        return EvseFeatureFlagsSnapshot(
            payload=(
                freeze_snapshot_mapping(self._payload)
                if self._payload is not None
                else None
            ),
            site_feature_flags=freeze_snapshot_mapping(self._site_feature_flags),
            charger_feature_flags_by_serial=cast(
                Mapping[str, Mapping[str, object]],
                freeze_snapshot_mapping(
                    {
                        str(key): freeze_snapshot_mapping(value)
                        for key, value in raw_by_serial.items()
                        if isinstance(value, dict)
                    }
                ),
            ),
            charger_serial_count=len(raw_by_serial),
        )

    @property
    def cache_until_mono(self) -> float | None:
        """Return the monotonic cache deadline."""

        return self._cache_until_mono

    @cache_until_mono.setter
    def cache_until_mono(self, value: float | None) -> None:
        self._cache_until_mono = value

    def replace_payload(self, value: object) -> None:
        """Replace the raw payload through the compatibility coordinator API."""

        self._payload = dict(value) if isinstance(value, dict) else None

    def replace_site_feature_flags(self, value: object) -> None:
        """Replace site flags through the compatibility coordinator API."""

        self._site_feature_flags = dict(value) if isinstance(value, dict) else {}

    def replace_charger_feature_flags(self, value: object) -> None:
        """Replace per-charger flags through the compatibility coordinator API."""

        self._charger_feature_flags_by_serial = (
            {str(key): item for key, item in value.items()}
            if isinstance(value, dict)
            else {}
        )

    def debug_feature_flag_summary(self) -> dict[str, object]:
        """Return a sanitized summary of EVSE feature-flag discovery."""

        return evse_feature_flag_debug_summary(self.snapshot)

    def _cached_state_present(self) -> bool:
        return bool(
            self._payload is not None
            or self._site_feature_flags
            or self._charger_feature_flags_by_serial
        )

    def refresh_due(self, *, force: bool = False) -> bool:
        """Return True when feature flags should refresh or cached state should clear."""

        now = time.monotonic()
        if not force and self._cache_until_mono:
            if now < self._cache_until_mono:
                return False
        fetcher = getattr(self.coordinator.client, "evse_feature_flags", None)
        if not callable(fetcher):
            return self._cached_state_present()
        return True

    def feature_flag(self, key: str, sn: str | None = None) -> object | None:
        """Return a parsed EVSE feature flag for the site or charger."""

        key_text = str(key).strip()
        if not key_text:
            return None
        if sn:
            serial_flags = self._charger_feature_flags_by_serial.get(str(sn))
            raw = serial_flags.get(key_text) if isinstance(serial_flags, dict) else None
            if raw is not None:
                return cast(object, raw)
        return self._site_feature_flags.get(key_text)

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

        self._site_feature_flags = {}
        self._charger_feature_flags_by_serial = {}
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
        self._site_feature_flags = site_flags
        self._charger_feature_flags_by_serial = dict(charger_flags)

    async def async_refresh(self, *, force: bool = False) -> None:
        """Refresh EVSE feature flags used for capability gating."""

        coord = self.coordinator
        now = time.monotonic()
        if not force and self._cache_until_mono:
            if now < self._cache_until_mono:
                return
        fetcher = getattr(coord.client, "evse_feature_flags", None)
        if not callable(fetcher):
            # Older client versions do not expose feature flags, so cached state
            # must clear.
            self._payload = None
            self._site_feature_flags = {}
            self._charger_feature_flags_by_serial = {}
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
            self._cache_until_mono = now + EVSE_FEATURE_FLAGS_FAILURE_BACKOFF_S
            return
        if not isinstance(payload, dict):
            self._payload = None
            self._site_feature_flags = {}
            self._charger_feature_flags_by_serial = {}
            self._cache_until_mono = now + EVSE_FEATURE_FLAGS_FAILURE_BACKOFF_S
            _LOGGER.debug(
                "EVSE feature flags payload shape was invalid: %s",
                debug_render_summary(debug_payload_shape(payload)),
            )
            return
        self._payload = dict(payload)
        self.parse_payload(payload)
        self._cache_until_mono = now + EVSE_FEATURE_FLAGS_CACHE_TTL
        coord._debug_log_summary_if_changed(
            "evse_feature_flags",
            "EVSE feature flag summary",
            self.debug_feature_flag_summary(),
        )
