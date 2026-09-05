"""Mutable EVSE cache ownership and immutable publication contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

type PayloadMap = dict[str, object]
type PayloadRecords = list[PayloadMap]
type PayloadMapByKey = dict[str, PayloadMap]


@dataclass(slots=True)
class EVSEState:
    _charge_mode_cache: dict[str, tuple[str, float]] = field(default_factory=dict)
    _green_battery_cache: dict[str, tuple[bool | None, bool, float]] = field(
        default_factory=dict
    )
    _green_battery_pending: dict[str, tuple[bool, float]] = field(default_factory=dict)
    _charger_config_cache: dict[str, tuple[PayloadMap, float]] = field(
        default_factory=dict
    )
    _charger_config_backoff_until: dict[str, float] = field(default_factory=dict)
    _auth_settings_cache: dict[
        str, tuple[bool | None, bool | None, bool, bool, float]
    ] = field(default_factory=dict)
    _app_auth_pending: dict[str, tuple[bool, float]] = field(default_factory=dict)
    _last_charging: dict[str, bool] = field(default_factory=dict)
    _last_actual_charging: dict[str, bool | None] = field(default_factory=dict)
    _pending_charging: dict[str, tuple[bool, float]] = field(default_factory=dict)
    _desired_charging: dict[str, bool] = field(default_factory=dict)
    _auto_resume_attempts: dict[str, float] = field(default_factory=dict)
    _session_end_fix: dict[str, int] = field(default_factory=dict)
    _evse_power_snapshots: PayloadMapByKey = field(default_factory=dict)
    _evse_transition_snapshots: dict[str, PayloadRecords] = field(default_factory=dict)

    def prune(self, serials: set[str]) -> None:
        """Discard removed devices from all runtime-owned per-charger caches."""

        for cache in (
            self._charge_mode_cache,
            self._green_battery_cache,
            self._green_battery_pending,
            self._charger_config_cache,
            self._charger_config_backoff_until,
            self._auth_settings_cache,
            self._app_auth_pending,
            self._last_charging,
            self._last_actual_charging,
            self._pending_charging,
            self._desired_charging,
            self._auto_resume_attempts,
            self._session_end_fix,
            self._evse_power_snapshots,
            self._evse_transition_snapshots,
        ):
            for serial in tuple(cache):
                if str(serial).strip() not in serials:
                    del cache[serial]

    def snapshot(self) -> EvseControlSnapshot:
        """Publish control values without cache bookkeeping timestamps."""

        return EvseControlSnapshot(
            desired_charging=MappingProxyType(dict(self._desired_charging)),
            pending_charging=MappingProxyType(
                {serial: value[0] for serial, value in self._pending_charging.items()}
            ),
            charge_modes=MappingProxyType(
                {serial: value[0] for serial, value in self._charge_mode_cache.items()}
            ),
            green_battery=MappingProxyType(
                {
                    serial: value[:2]
                    for serial, value in self._green_battery_cache.items()
                }
            ),
            auth_settings=MappingProxyType(
                {
                    serial: value[:4]
                    for serial, value in self._auth_settings_cache.items()
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class EvseControlSnapshot:
    """Immutable EVSE command/capability values used in update equality."""

    desired_charging: Mapping[str, bool] = field(
        default_factory=lambda: MappingProxyType({})
    )
    pending_charging: Mapping[str, bool] = field(
        default_factory=lambda: MappingProxyType({})
    )
    charge_modes: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    green_battery: Mapping[str, tuple[bool | None, bool]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    auth_settings: Mapping[str, tuple[bool | None, bool | None, bool, bool]] = field(
        default_factory=lambda: MappingProxyType({})
    )
