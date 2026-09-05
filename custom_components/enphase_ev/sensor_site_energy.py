"""Site energy and power sensor models with native restore state."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .device_info_helpers import _cloud_device_info
from .energy import SiteEnergyFlow
from .power_validation import EXTREME_SITE_POWER_W, ExtremePowerValidator
from .runtime_helpers import inventory_type_device_info as _type_device_info
from .sensor_base import EnphaseSiteSensorEntity as _SiteBaseEntity
from .sensor_common import (
    _energy_delta_to_power_w,
    _has_type,
    _lifetime_energy_delta,
    _normalize_utc_datetime,
    _resolve_lifetime_power_window,
    _restore_optional_float_attribute,
    _restore_optional_int_value,
)
from .sensor_snapshot_helpers import restore_power_w

CURRENT_POWER_CACHE_TTL_MULTIPLIER = 2


SITE_LIFETIME_FLOW_BUCKET_LENGTH_KEYS: dict[str, tuple[str, ...]] = {
    "grid_import": ("import", "grid_home", "grid_battery"),
    "grid_export": ("solar_grid", "battery_grid", "generator_grid"),
    "battery_charge": ("charge", "solar_battery", "grid_battery"),
    "battery_discharge": ("discharge", "battery_home", "battery_grid"),
}


@dataclass
class _SiteEnergyRestoreData(SensorExtraStoredData):  # type: ignore[misc]
    """Persist sensor state and site-energy composition migration state."""

    composition_offset_kwh: float
    composition_offset_reset_at: str | None
    composition_source_start_date: str | None
    composition_migration_checked: bool

    def as_dict(self) -> dict[str, object]:
        return {
            **super().as_dict(),
            "composition_offset_kwh": self.composition_offset_kwh,
            "composition_offset_reset_at": self.composition_offset_reset_at,
            "composition_source_start_date": self.composition_source_start_date,
            "composition_migration_checked": self.composition_migration_checked,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "_SiteEnergyRestoreData":
        if not isinstance(data, dict):
            return cls(None, None, 0.0, None, None, False)
        sensor_restore = SensorExtraStoredData.from_dict(data)
        try:
            offset = float(data.get("composition_offset_kwh", 0.0))
        except (TypeError, ValueError):
            offset = 0.0
        if not math.isfinite(offset) or offset < 0:
            offset = 0.0
        reset_at = data.get("composition_offset_reset_at")
        start_date = data.get("composition_source_start_date")
        return cls(
            native_value=(
                sensor_restore.native_value if sensor_restore is not None else None
            ),
            native_unit_of_measurement=(
                sensor_restore.native_unit_of_measurement
                if sensor_restore is not None
                else None
            ),
            composition_offset_kwh=offset,
            composition_offset_reset_at=(
                reset_at if isinstance(reset_at, str) else None
            ),
            composition_source_start_date=(
                start_date if isinstance(start_date, str) else None
            ),
            composition_migration_checked=(
                data.get("composition_migration_checked") is True
            ),
        )


@dataclass
class _SiteLifetimePowerRestoreData(ExtraStoredData):  # type: ignore[misc]
    """Persist the last two live lifetime-energy samples across restarts."""

    previous_live_flow_kwh: dict[str, float]
    previous_live_energy_ts: float | None
    previous_live_sample_ts: float | None
    last_live_interval_minutes: float | None
    last_live_flow_sources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    previous_live_flow_sources: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "previous_live_flow_kwh": dict(self.previous_live_flow_kwh),
            "previous_live_energy_ts": self.previous_live_energy_ts,
            "previous_live_sample_ts": self.previous_live_sample_ts,
            "last_live_interval_minutes": self.last_live_interval_minutes,
            "last_live_flow_sources": {
                key: list(value) for key, value in self.last_live_flow_sources.items()
            },
            "previous_live_flow_sources": {
                key: list(value)
                for key, value in self.previous_live_flow_sources.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "_SiteLifetimePowerRestoreData":
        if not isinstance(data, dict):
            return cls({}, None, None, None)

        previous_live_flow_kwh: dict[str, float] = {}
        raw_previous_live_flow_kwh = data.get("previous_live_flow_kwh")
        if isinstance(raw_previous_live_flow_kwh, dict):
            for flow_key, raw_value in raw_previous_live_flow_kwh.items():
                if not isinstance(flow_key, str):
                    continue
                try:
                    numeric = float(raw_value)
                except Exception:
                    continue
                if numeric < 0:
                    continue
                previous_live_flow_kwh[flow_key] = numeric

        def _as_float(value: object) -> float | None:
            try:
                return float(value) if value is not None else None  # type: ignore[arg-type]
            except Exception:
                return None

        def _source_map(key: str) -> dict[str, tuple[str, ...]]:
            raw_map = data.get(key)
            if not isinstance(raw_map, dict):
                return {}
            parsed: dict[str, tuple[str, ...]] = {}
            for raw_flow_key, raw_sources in raw_map.items():
                if not isinstance(raw_flow_key, str) or not isinstance(
                    raw_sources, (list, tuple)
                ):
                    continue
                sources = tuple(
                    sorted(
                        {
                            str(source).strip()
                            for source in raw_sources
                            if str(source).strip()
                        }
                    )
                )
                if sources:
                    parsed[raw_flow_key] = sources
            return parsed

        return cls(
            previous_live_flow_kwh=previous_live_flow_kwh,
            previous_live_energy_ts=_as_float(data.get("previous_live_energy_ts")),
            previous_live_sample_ts=_as_float(data.get("previous_live_sample_ts")),
            last_live_interval_minutes=_as_float(
                data.get("last_live_interval_minutes")
            ),
            last_live_flow_sources=_source_map("last_live_flow_sources"),
            previous_live_flow_sources=_source_map("previous_live_flow_sources"),
        )


@dataclass
class _SiteConsumptionPowerRestoreData(ExtraStoredData):  # type: ignore[misc]
    """Persist a validated site-consumption bucket baseline across restarts."""

    latest_bucket_wh: float | None
    raw_bucket_count: int | None
    start_date: str | None
    energy_ts: float | None
    interval_minutes: float | None
    last_power_w: int | None
    last_window_seconds: float | None
    method: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "latest_bucket_wh": self.latest_bucket_wh,
            "raw_bucket_count": self.raw_bucket_count,
            "start_date": self.start_date,
            "energy_ts": self.energy_ts,
            "interval_minutes": self.interval_minutes,
            "last_power_w": self.last_power_w,
            "last_window_seconds": self.last_window_seconds,
            "method": self.method,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> _SiteConsumptionPowerRestoreData:
        if not isinstance(data, dict):
            return cls(None, None, None, None, None, None, None, None)

        def _as_float(value: object) -> float | None:
            try:
                numeric = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
            return numeric if math.isfinite(numeric) else None

        def _as_int(value: object) -> int | None:
            return _restore_optional_int_value(value)

        start_date = data.get("start_date")
        method = data.get("method")
        return cls(
            latest_bucket_wh=_as_float(data.get("latest_bucket_wh")),
            raw_bucket_count=_as_int(data.get("raw_bucket_count")),
            start_date=start_date if isinstance(start_date, str) else None,
            energy_ts=_as_float(data.get("energy_ts")),
            interval_minutes=_as_float(data.get("interval_minutes")),
            last_power_w=_as_int(data.get("last_power_w")),
            last_window_seconds=_as_float(data.get("last_window_seconds")),
            method=method if isinstance(method, str) else None,
        )


class EnphaseSiteEnergySensor(_SiteBaseEntity, RestoreSensor):  # type: ignore[misc]
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coord: EnphaseCoordinator,
        flow_key: str,
        translation_key: str,
        name: str,
    ) -> None:
        super().__init__(coord, flow_key, name)
        self._flow_key = flow_key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{flow_key}"
        self._restored_value: float | None = None
        self._restored_reset_at: str | None = None
        self._composition_offset_kwh = 0.0
        self._composition_offset_reset_at: str | None = None
        self._composition_source_start_date: str | None = None
        self._composition_migration_checked = flow_key != "grid_export"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None:
            try:
                restored = (
                    float(last.native_value) if last.native_value is not None else None
                )
            except Exception:  # noqa: BLE001
                restored = None
            if restored is not None and restored >= 0:
                self._restored_value = restored
                self._attr_native_value = restored
        try:
            last_state = await self.async_get_last_state()
        except Exception:  # noqa: BLE001
            last_state = None
        if last_state is not None:
            attributes = last_state.attributes or {}
            reset_attr = attributes.get("last_reset_at")
            if isinstance(reset_attr, str):
                self._restored_reset_at = reset_attr
        last_extra = await self.async_get_last_extra_data()
        composition_restore = _SiteEnergyRestoreData.from_dict(
            last_extra.as_dict() if last_extra is not None else None
        )
        if (
            self._flow_key == "grid_export"
            and composition_restore.composition_migration_checked
        ):
            self._composition_offset_kwh = composition_restore.composition_offset_kwh
            self._composition_offset_reset_at = (
                composition_restore.composition_offset_reset_at
            )
            self._composition_source_start_date = (
                composition_restore.composition_source_start_date
            )
            self._composition_migration_checked = True

    @staticmethod
    def _coerce_nonnegative_float(value: object) -> float | None:
        try:
            numeric = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) and numeric >= 0 else None

    def _flow_data(self) -> dict[str, object]:
        energy = getattr(self._coord, "energy", None)
        flows = (
            getattr(energy, "site_energy", None)
            if energy is not None
            else getattr(self._coord, "site_energy", None)
        ) or {}
        entry = flows.get(self._flow_key)
        if isinstance(entry, SiteEnergyFlow):
            return {
                "value_kwh": entry.value_kwh,
                "bucket_count": entry.bucket_count,
                "fields_used": entry.fields_used,
                "start_date": entry.start_date,
                "last_report_date": entry.last_report_date,
                "update_pending": entry.update_pending,
                "source_unit": entry.source_unit,
                "last_reset_at": entry.last_reset_at,
                "interval_minutes": entry.interval_minutes,
                "legacy_offset_kwh": entry.legacy_offset_kwh,
            }
        if isinstance(entry, dict):
            return entry
        return {}

    def _current_value(self) -> float | None:
        data = self._flow_data()
        val = data.get("value_kwh")
        if val is None:
            return None
        try:
            return float(val)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None

    def _ensure_composition_migration(self) -> None:
        """Preserve total-increasing continuity across export composition changes."""

        data = self._flow_data()
        current_value = self._coerce_nonnegative_float(data.get("value_kwh"))
        reset_at = data.get("last_reset_at")
        current_reset_at = reset_at if isinstance(reset_at, str) else None
        start_date = data.get("start_date")
        current_start_date = start_date if isinstance(start_date, str) else None
        if self._composition_migration_checked:
            reset_marker_changed = (
                current_reset_at is not None
                and current_reset_at != self._composition_offset_reset_at
            )
            source_start_changed = (
                current_start_date is not None
                and self._composition_source_start_date is not None
                and current_start_date != self._composition_source_start_date
            )
            offset_exceeds_total = (
                current_value is not None
                and current_value < self._composition_offset_kwh
            )
            if reset_marker_changed or source_start_changed or offset_exceeds_total:
                self._composition_offset_kwh = 0.0
                self._composition_offset_reset_at = current_reset_at
                self._composition_source_start_date = current_start_date
            return
        if current_value is None:
            return
        if self._restored_value is not None:
            legacy_offset = self._coerce_nonnegative_float(
                data.get("legacy_offset_kwh")
            )
            if legacy_offset is not None:
                legacy_value = max(current_value - legacy_offset, 0.0)
                restored_to_legacy = abs(self._restored_value - legacy_value)
                restored_to_composite = abs(self._restored_value - current_value)
                # A takeover can restore a total from another integration. Only
                # remove the newly added channels when the prior value actually
                # tracks the legacy solar-only composition.
                if restored_to_legacy <= restored_to_composite:
                    self._composition_offset_kwh = legacy_offset
        self._composition_offset_reset_at = current_reset_at
        self._composition_source_start_date = current_start_date
        self._composition_migration_checked = True

    @property
    def available(self) -> bool:
        if self._coord.last_success_utc is None and not bool(
            getattr(self._coord, "last_update_success", False)
        ):
            return False
        if self._current_value() is not None:
            return True
        return self._restored_value is not None

    @property
    def device_info(self) -> Any:
        heatpump_available = self._flow_key == "heat_pump" and _has_type(
            self._coord, "heatpump"
        )
        if self._flow_key == "heat_pump" and heatpump_available:
            heatpump_info = _type_device_info(self._coord, "heatpump")
            if heatpump_info is not None:
                return heatpump_info
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)

    @property
    def native_value(self) -> Any:
        current = self._current_value()
        if current is not None:
            self._ensure_composition_migration()
            return round(max(current - self._composition_offset_kwh, 0.0), 2)
        if self._restored_value is None:
            return None
        return round(self._restored_value, 2)

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        if self._flow_key != "grid_export":
            return super().extra_restore_state_data
        sensor_restore = super().extra_restore_state_data
        return _SiteEnergyRestoreData(
            native_value=sensor_restore.native_value,
            native_unit_of_measurement=sensor_restore.native_unit_of_measurement,
            composition_offset_kwh=self._composition_offset_kwh,
            composition_offset_reset_at=self._composition_offset_reset_at,
            composition_source_start_date=self._composition_source_start_date,
            composition_migration_checked=self._composition_migration_checked,
        )

    @property
    def extra_state_attributes(self) -> Any:
        data = self._flow_data()
        self._ensure_composition_migration()
        attrs: dict[str, object] = {}
        last_report_raw = data.get("last_report_date")
        parsed_sample_ts = _EnphaseSiteLifetimePowerSensor._parse_sample_timestamp(
            last_report_raw
        )
        if parsed_sample_ts is not None:
            attrs["sampled_at_utc"] = datetime.fromtimestamp(
                parsed_sample_ts, tz=timezone.utc
            ).isoformat()

        reset_at = data.get("last_reset_at") or self._restored_reset_at
        if reset_at:
            attrs["last_reset_at"] = reset_at

        if self._flow_key != "heat_pump":
            return attrs

        heatpump_power = getattr(self._coord, "heatpump_power_w", None)
        if heatpump_power is not None:
            try:
                attrs["heat_pump_power_w"] = round(float(heatpump_power), 3)
            except Exception:  # noqa: BLE001
                attrs["heat_pump_power_w"] = None

        daily = getattr(self._coord, "heatpump_daily_consumption", None)
        if not isinstance(daily, dict):
            return attrs

        for key in (
            "daily_energy_wh",
            "daily_solar_wh",
            "daily_battery_wh",
            "daily_grid_wh",
            "device_uid",
            "device_name",
            "member_name",
            "member_device_type",
            "pairing_status",
            "device_state",
            "endpoint_type",
            "endpoint_timestamp",
            "day_key",
            "timezone",
            "source",
        ):
            if key not in daily:
                continue
            attr_key = {
                "device_uid": "daily_device_uid",
                "device_name": "daily_device_name",
                "member_name": "daily_member_name",
                "member_device_type": "daily_member_device_type",
                "pairing_status": "daily_pairing_status",
                "device_state": "daily_device_state",
                "endpoint_type": "daily_endpoint_type",
                "endpoint_timestamp": "daily_endpoint_timestamp",
                "source": "daily_source",
            }.get(key, key)
            attrs[attr_key] = daily.get(key)

        return attrs


class _EnphaseSiteLifetimePowerSensor(_SiteBaseEntity, RestoreEntity):  # type: ignore[misc]
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes | frozenset(
        {
            "last_flow_kwh",
            "last_energy_ts",
            "last_sample_ts",
            "last_power_w",
            "last_window_seconds",
            "last_report_date",
            "last_reset_at",
            "method",
            "source_flows",
        }
    )

    _DEFAULT_WINDOW_S = 300.0
    _MIN_DELTA_KWH = 0.0005
    _RESET_DROP_KWH = 0.25
    _OUTLIER_MIN_POWER_W = 100_000
    _OUTLIER_MIN_DELTA_KWH = 5.0
    _OUTLIER_RELATIVE_FLOW_RATIO = 0.15

    def __init__(
        self,
        coord: EnphaseCoordinator,
        key: str,
        name: str,
        *,
        translation_key: str,
        flow_signs: dict[str, int],
        type_key: str | None = None,
    ) -> None:
        super().__init__(coord, key, name, type_key=type_key)
        self._attr_translation_key = translation_key
        self._flow_signs = dict(flow_signs)
        self._last_flow_kwh: dict[str, float] = {}
        self._last_energy_ts: float | None = None
        self._last_sample_ts: float | None = None
        self._last_power_w: int = 0
        self._last_window_s: float | None = None
        self._last_method: str = "seeded"
        self._last_reset_at: float | None = None
        self._last_report_date_iso: str | None = None
        self._restored_power_w: int | None = None
        self._synthetic_zero_flows: set[str] = set()
        self._live_flow_sample_count: int = 0
        self._previous_live_flow_kwh: dict[str, float] = {}
        self._previous_live_energy_ts: float | None = None
        self._previous_live_sample_ts: float | None = None
        self._last_live_interval_minutes: float | None = None
        self._last_live_flow_sources: dict[str, tuple[str, ...]] = {}
        self._previous_live_flow_sources: dict[str, tuple[str, ...]] = {}
        self._extreme_power_validator = ExtremePowerValidator()
        self._restored_method_explicit = False

    def _clear_restored_live_history(self, *, discard_power: bool = False) -> None:
        """Drop restored live-history samples that are not safe to reuse."""

        self._previous_live_flow_kwh = {}
        self._previous_live_energy_ts = None
        self._previous_live_sample_ts = None
        self._previous_live_flow_sources = {}
        if discard_power:
            self._restored_power_w = None

    def _discard_restored_baseline(self) -> None:
        """Drop a restored baseline sample that is not safe to reuse."""

        self._last_flow_kwh = {}
        self._last_energy_ts = None
        self._last_sample_ts = None
        self._last_window_s = None
        self._last_live_flow_sources = {}
        self._extreme_power_validator.clear()
        self._last_method = "seeded"

    def _restored_flows_zeroed(self, flows: dict[str, float]) -> bool:
        """Return True when every restored flow is effectively zero."""

        return bool(flows) and all(
            abs(value) <= self._MIN_DELTA_KWH for value in flows.values()
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        last_extra = await self.async_get_last_extra_data()
        extra_data = _SiteLifetimePowerRestoreData.from_dict(
            last_extra.as_dict() if last_extra is not None else None
        )
        if not last_state:
            return
        attrs = last_state.attributes or {}
        raw_last_flow_kwh = attrs.get("last_flow_kwh")
        if isinstance(raw_last_flow_kwh, dict):
            restored_flows: dict[str, float] = {}
            for flow_key in self._flow_signs:
                raw_value = raw_last_flow_kwh.get(flow_key)
                try:
                    if raw_value is not None:
                        restored_flows[flow_key] = float(raw_value)
                except Exception:
                    continue
            self._last_flow_kwh = restored_flows
        self._last_energy_ts = _restore_optional_float_attribute(
            attrs, "last_energy_ts"
        )
        self._last_sample_ts = _restore_optional_float_attribute(
            attrs, "last_sample_ts"
        )
        restored_power = restore_power_w(last_state)
        if restored_power is None:
            self._restored_power_w = None
        else:
            self._last_power_w = restored_power
            self._restored_power_w = restored_power
            self._last_power_w = 0
        attr_power = _restore_optional_int_value(attrs.get("last_power_w"))
        if attr_power is not None:
            self._restored_power_w = attr_power
        self._last_window_s = _restore_optional_float_attribute(
            attrs, "last_window_seconds"
        )
        self._last_reset_at = _restore_optional_float_attribute(attrs, "last_reset_at")
        last_method = attrs.get("method")
        if isinstance(last_method, str) and last_method.strip():
            self._last_method = last_method
            self._restored_method_explicit = True
        last_report_date = attrs.get("last_report_date")
        if isinstance(last_report_date, str) and last_report_date.strip():
            self._last_report_date_iso = last_report_date
        self._previous_live_flow_kwh = {
            flow_key: value
            for flow_key, value in extra_data.previous_live_flow_kwh.items()
            if flow_key in self._flow_signs
        }
        self._previous_live_energy_ts = extra_data.previous_live_energy_ts
        self._previous_live_sample_ts = extra_data.previous_live_sample_ts
        self._last_live_interval_minutes = extra_data.last_live_interval_minutes
        self._last_live_flow_sources = {
            flow_key: sources
            for flow_key, sources in extra_data.last_live_flow_sources.items()
            if flow_key in self._flow_signs
        }
        self._previous_live_flow_sources = {
            flow_key: sources
            for flow_key, sources in extra_data.previous_live_flow_sources.items()
            if flow_key in self._flow_signs
        }
        self._restore_live_history()

    def _restore_live_history(self) -> None:
        """Restore a valid two-sample live history when available."""

        restored_baseline_zeroed = self._restored_flows_zeroed(self._last_flow_kwh)
        restored_previous_zeroed = self._restored_flows_zeroed(
            self._previous_live_flow_kwh
        )

        if (
            self._last_flow_kwh
            and self._previous_live_flow_kwh
            and (
                any(
                    flow_key not in self._last_live_flow_sources
                    for flow_key in self._last_flow_kwh
                )
                or any(
                    flow_key not in self._previous_live_flow_sources
                    for flow_key in self._previous_live_flow_kwh
                )
            )
        ):
            self._clear_restored_live_history(discard_power=True)
            self._discard_restored_baseline()
            return

        if any(
            self._last_live_flow_sources.get(flow_key)
            != self._previous_live_flow_sources.get(flow_key)
            for flow_key in self._flow_signs
            if flow_key in self._last_flow_kwh
            and flow_key in self._previous_live_flow_kwh
        ):
            self._clear_restored_live_history(discard_power=True)
            self._discard_restored_baseline()
            return

        if self._restored_method_explicit and self._last_method in {
            "seeded",
            "no_live_data",
        }:
            self._clear_restored_live_history(discard_power=True)
            return

        if restored_baseline_zeroed and not self._restored_method_explicit:
            self._clear_restored_live_history(discard_power=True)
            return

        if (
            self._restored_method_explicit
            and self._last_method in {"lifetime_reset", "restored_lifetime_reset"}
            and restored_baseline_zeroed
        ):
            self._clear_restored_live_history(discard_power=True)
            return

        if (
            restored_previous_zeroed
            and not restored_baseline_zeroed
            and any(
                value > self._RESET_DROP_KWH for value in self._last_flow_kwh.values()
            )
        ):
            self._clear_restored_live_history(discard_power=True)
            return

        if (
            not self._last_flow_kwh
            or not self._previous_live_flow_kwh
            or self._last_sample_ts is None
            or self._previous_live_sample_ts is None
            or self._previous_live_sample_ts >= self._last_sample_ts
        ):
            self._clear_restored_live_history(discard_power=True)
            return

        reset_detected = False
        signed_delta_kwh = 0.0
        for flow_key, sign in self._flow_signs.items():
            current = self._last_flow_kwh.get(flow_key)
            previous = self._previous_live_flow_kwh.get(flow_key)
            if current is None or previous is None:
                continue
            delta, flow_reset = _lifetime_energy_delta(
                current_kwh=current,
                previous_kwh=previous,
                reset_drop_kwh=self._RESET_DROP_KWH,
            )
            if flow_reset:
                reset_detected = True
                break
            if delta is not None:
                signed_delta_kwh += delta * sign

        if reset_detected:
            self._last_power_w = 0
            self._last_method = "restored_lifetime_reset"
            self._last_reset_at = self._last_sample_ts
        else:
            window_s = _resolve_lifetime_power_window(
                sample_ts=self._last_sample_ts,
                previous_energy_ts=(
                    self._previous_live_energy_ts or self._previous_live_sample_ts
                ),
                default_window_s=self._DEFAULT_WINDOW_S,
            )
            window_s = max(window_s, self._DEFAULT_WINDOW_S)
            if self._last_live_interval_minutes is not None:
                window_s = max(window_s, self._last_live_interval_minutes * 60.0)
            self._last_window_s = window_s
            if abs(signed_delta_kwh) <= self._MIN_DELTA_KWH:
                self._last_power_w = 0
                self._last_method = "restored_no_change"
            else:
                restored_power_w = _energy_delta_to_power_w(
                    signed_delta_kwh,
                    window_s=window_s,
                )
                if abs(restored_power_w) >= EXTREME_SITE_POWER_W:
                    self._clear_restored_live_history(discard_power=True)
                    self._discard_restored_baseline()
                    return
                if not self._power_sample_is_plausible(
                    power_w=restored_power_w,
                    signed_delta_kwh=signed_delta_kwh,
                    current_values=self._last_flow_kwh,
                    previous_values=self._previous_live_flow_kwh,
                ):
                    self._clear_restored_live_history(discard_power=True)
                    self._discard_restored_baseline()
                    return
                self._last_power_w = restored_power_w
                self._last_method = "restored_lifetime_energy_window"

        self._restored_power_w = self._last_power_w
        self._live_flow_sample_count = 2

    @staticmethod
    def _coerce_flow_value(entry: object) -> float | None:
        value = None
        if isinstance(entry, SiteEnergyFlow):
            value = entry.value_kwh
        elif isinstance(entry, dict):
            value = entry.get("value_kwh")
        if value is None:
            return None
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric < 0:
            return None
        return round(numeric, 3)

    @staticmethod
    def _parse_sample_timestamp(raw: object) -> float | None:
        if raw is None:
            return None
        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                return raw.replace(tzinfo=timezone.utc).timestamp()
            return raw.astimezone(timezone.utc).timestamp()
        if isinstance(raw, (int, float)):
            value = float(raw)
            if value > 10**12:
                value = value / 1000.0
            return value if value > 0 else None
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return None
            if stripped.isdigit():
                return _EnphaseSiteLifetimePowerSensor._parse_sample_timestamp(
                    int(stripped)
                )
            normalized = stripped.replace("[UTC]", "").replace("Z", "+00:00")
            try:
                dt_obj = datetime.fromisoformat(normalized)
            except ValueError:
                dt_obj = dt_util.parse_datetime(stripped)
            if dt_obj is None:
                try:
                    date_obj = dt_util.parse_date(stripped)
                except Exception:
                    date_obj = None
                if date_obj is None:
                    return None
                dt_obj = datetime.combine(
                    date_obj, datetime.min.time(), tzinfo=timezone.utc
                )
            elif dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=timezone.utc)
            return dt_obj.astimezone(timezone.utc).timestamp()
        return None

    def _site_energy_flows(self) -> dict[str, object]:
        energy = getattr(self._coord, "energy", None)
        flows = (
            getattr(energy, "site_energy", None)
            if energy is not None
            else getattr(self._coord, "site_energy", None)
        )
        return flows if isinstance(flows, dict) else {}

    def _site_energy_meta(self) -> dict[str, object]:
        energy = getattr(self._coord, "energy", None)
        meta = (
            getattr(energy, "site_energy_meta", None)
            if energy is not None
            else getattr(self._coord, "site_energy_meta", None)
        )
        return meta if isinstance(meta, dict) else {}

    @classmethod
    def _power_sample_is_plausible(
        self,
        *,
        power_w: int,
        signed_delta_kwh: float,
        current_values: dict[str, float],
        previous_values: dict[str, float],
    ) -> bool:
        """Reject obviously corrupt cumulative-delta samples without capping large sites."""

        abs_power_w = abs(power_w)
        abs_delta_kwh = abs(signed_delta_kwh)
        if (
            abs_power_w < self._OUTLIER_MIN_POWER_W
            or abs_delta_kwh < self._OUTLIER_MIN_DELTA_KWH
        ):
            return True

        flow_scale_kwh = 0.0
        for value in (*current_values.values(), *previous_values.values()):
            try:
                numeric = abs(float(value))
            except Exception:
                continue
            flow_scale_kwh = max(flow_scale_kwh, numeric)

        if flow_scale_kwh <= 0:
            return True

        return abs_delta_kwh < (flow_scale_kwh * self._OUTLIER_RELATIVE_FLOW_RATIO)

    def _flow_supported(self, flow_key: str) -> bool:
        if flow_key in self._site_energy_flows():
            return True

        known_channel = getattr(
            getattr(self._coord, "discovery_snapshot", None),
            "site_energy_channel_known",
            None,
        )
        if callable(known_channel):
            try:
                if known_channel(flow_key):
                    return True
            except Exception:  # noqa: BLE001
                pass

        bucket_lengths = self._site_energy_meta().get("bucket_lengths")
        if not isinstance(bucket_lengths, dict):
            return False
        for bucket_key in SITE_LIFETIME_FLOW_BUCKET_LENGTH_KEYS.get(
            flow_key, (flow_key,)
        ):
            bucket_length = bucket_lengths.get(bucket_key)
            try:
                if int(bucket_length) > 0:  # type: ignore[arg-type]
                    return True
            except (TypeError, ValueError):
                if bucket_length:
                    return True
        return False

    def _current_flow_values(self) -> tuple[dict[str, float], set[str]]:
        flows = self._site_energy_flows()
        values: dict[str, float] = {}
        synthetic_zero_flows: set[str] = set()
        for flow_key in self._flow_signs:
            current = self._coerce_flow_value(flows.get(flow_key))
            if current is not None:
                values[flow_key] = current
            elif self._flow_supported(flow_key):
                values[flow_key] = 0.0
                synthetic_zero_flows.add(flow_key)
        return values, synthetic_zero_flows

    def _current_flow_sources(
        self, flows: dict[str, object], current_values: dict[str, float]
    ) -> dict[str, tuple[str, ...]]:
        """Return normalized cumulative-source signatures for live flows."""

        signatures: dict[str, tuple[str, ...]] = {}
        for flow_key in self._flow_signs:
            if flow_key not in current_values:
                continue
            entry = flows.get(flow_key)
            fields_used: object = None
            if isinstance(entry, SiteEnergyFlow):
                fields_used = entry.fields_used
            elif isinstance(entry, dict):
                fields_used = entry.get("fields_used")
            if not isinstance(fields_used, (list, tuple)):
                continue
            normalized: set[str] = set()
            for raw_field in fields_used:
                try:
                    field_text = str(raw_field).strip()
                except Exception:
                    continue
                if field_text:
                    normalized.add(field_text)
            if normalized:
                signatures[flow_key] = tuple(sorted(normalized))
        return signatures

    def _flow_source_changed(
        self,
        current_values: dict[str, float],
        current_sources: dict[str, tuple[str, ...]],
    ) -> bool:
        """Return True when a contributing cumulative channel changed source."""

        for flow_key in self._flow_signs:
            if flow_key not in current_values or flow_key not in self._last_flow_kwh:
                continue
            previous = self._last_live_flow_sources.get(flow_key)
            current = current_sources.get(flow_key)
            if previous is not None and current is not None and previous != current:
                return True
        return False

    @staticmethod
    def _has_live_flow_values(
        current_values: dict[str, float], synthetic_zero_flows: set[str]
    ) -> bool:
        """Return True when at least one flow has a live reading."""

        return any(flow_key not in synthetic_zero_flows for flow_key in current_values)

    def _source_sample_timestamp(self, flows: dict[str, object]) -> float | None:
        """Return only a timestamp reported by the site-energy payload."""

        for flow_key in self._flow_signs:
            entry = flows.get(flow_key)
            raw_report_date = None
            if isinstance(entry, SiteEnergyFlow):
                raw_report_date = entry.last_report_date
            elif isinstance(entry, dict):
                raw_report_date = entry.get("last_report_date")
            parsed = self._parse_sample_timestamp(raw_report_date)
            if parsed is not None:
                return parsed

        meta_report_date = self._site_energy_meta().get("last_report_date")
        return self._parse_sample_timestamp(meta_report_date)

    def _sample_timestamp(self, flows: dict[str, object]) -> tuple[float, str | None]:
        parsed = self._source_sample_timestamp(flows)
        if parsed is not None:
            iso = datetime.fromtimestamp(parsed, tz=timezone.utc).isoformat()
            return parsed, iso

        last_success_utc = getattr(self._coord, "last_success_utc", None)
        parsed = self._parse_sample_timestamp(last_success_utc)
        if parsed is not None:
            iso = datetime.fromtimestamp(parsed, tz=timezone.utc).isoformat()
            return parsed, iso

        now = dt_util.utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.timestamp(), now.isoformat()

    @staticmethod
    def _coerce_interval_minutes(raw: object) -> float | None:
        try:
            interval_minutes = float(raw)  # type: ignore[arg-type]
        except Exception:
            return None
        return interval_minutes if interval_minutes > 0 else None

    def _minimum_window_seconds(
        self,
        flows: dict[str, object],
        current_values: dict[str, float],
    ) -> float | None:
        interval_minutes_values: list[float] = []

        for flow_key in self._flow_signs:
            if flow_key not in current_values:
                continue
            entry = flows.get(flow_key)
            raw_interval = None
            if isinstance(entry, SiteEnergyFlow):
                raw_interval = entry.interval_minutes
            elif isinstance(entry, dict):
                raw_interval = entry.get("interval_minutes")
            interval_minutes = self._coerce_interval_minutes(raw_interval)
            if interval_minutes is not None:
                interval_minutes_values.append(interval_minutes)

        meta_interval_minutes = self._coerce_interval_minutes(
            self._site_energy_meta().get("interval_minutes")
        )
        if meta_interval_minutes is not None:
            interval_minutes_values.append(meta_interval_minutes)

        if not interval_minutes_values:
            return None
        return max(interval_minutes_values) * 60.0

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        current_values, _synthetic_zero_flows = self._current_flow_values()
        return bool(current_values)

    @property
    def native_value(self) -> Any:
        flows = self._site_energy_flows()
        current_values, synthetic_zero_flows = self._current_flow_values()
        has_live_flow_values = self._has_live_flow_values(
            current_values, synthetic_zero_flows
        )

        source_sample_ts = self._source_sample_timestamp(flows)
        sample_ts, sample_iso = self._sample_timestamp(flows)
        self._last_report_date_iso = sample_iso
        if self._last_sample_ts is not None and sample_ts == self._last_sample_ts:
            if self._live_flow_sample_count >= 2:
                return self._last_power_w
            return None

        if self._last_flow_kwh:
            for flow_key in self._flow_signs:
                if flow_key in current_values or flow_key not in self._last_flow_kwh:
                    continue
                current_values[flow_key] = 0.0
                synthetic_zero_flows.add(flow_key)

        current_sources = self._current_flow_sources(flows, current_values)
        self._synthetic_zero_flows = synthetic_zero_flows
        if not current_values:
            return None
        if not has_live_flow_values:
            if self._last_flow_kwh:
                self._last_flow_kwh.update(current_values)
                self._last_energy_ts = sample_ts
                self._last_sample_ts = sample_ts
                self._last_live_flow_sources.update(current_sources)
                self._last_power_w = 0
                self._last_method = "no_live_data"
                self._last_window_s = None
            else:
                self._last_sample_ts = sample_ts
                self._last_power_w = 0
                self._last_method = "seeded"
                self._last_window_s = None
            return 0

        if self._live_flow_sample_count == 0:
            self._previous_live_flow_kwh = {}
            self._previous_live_energy_ts = None
            self._previous_live_sample_ts = None
            self._previous_live_flow_sources = {}
            self._last_flow_kwh = dict(current_values)
            self._last_live_flow_sources = dict(current_sources)
            self._last_energy_ts = sample_ts
            self._last_sample_ts = sample_ts
            self._last_power_w = 0
            self._last_method = "seeded"
            self._last_window_s = None
            self._live_flow_sample_count = 1
            return None

        if not self._last_flow_kwh:
            self._previous_live_flow_kwh = {}
            self._previous_live_energy_ts = None
            self._previous_live_sample_ts = None
            self._previous_live_flow_sources = {}
            self._last_flow_kwh = dict(current_values)
            self._last_live_flow_sources = dict(current_sources)
            self._last_energy_ts = sample_ts
            self._last_sample_ts = sample_ts
            self._last_power_w = 0
            self._last_method = "seeded"
            self._last_window_s = None
            return None

        prior_last_power_w = self._last_power_w
        prior_live_sample_count = self._live_flow_sample_count

        if self._flow_source_changed(current_values, current_sources):
            self._previous_live_flow_kwh = dict(self._last_flow_kwh)
            self._previous_live_energy_ts = self._last_energy_ts
            self._previous_live_sample_ts = self._last_sample_ts
            self._previous_live_flow_sources = dict(self._last_live_flow_sources)
            self._last_flow_kwh = dict(current_values)
            self._last_live_flow_sources = dict(current_sources)
            self._last_energy_ts = sample_ts
            self._last_sample_ts = sample_ts
            self._last_window_s = None
            self._last_method = "source_changed_reseed"
            self._live_flow_sample_count += 1
            self._extreme_power_validator.clear()
            return prior_last_power_w if prior_live_sample_count >= 2 else None

        reset_detected = False
        signed_delta_kwh = 0.0
        previous_live_flow_kwh = dict(self._last_flow_kwh)
        previous_live_energy_ts = self._last_energy_ts
        previous_live_sample_ts = self._last_sample_ts
        previous_live_flow_sources = dict(self._last_live_flow_sources)
        for flow_key, sign in self._flow_signs.items():
            current = current_values.get(flow_key)
            if current is None:
                continue
            previous = self._last_flow_kwh.get(flow_key)
            if previous is None:
                continue
            if flow_key in synthetic_zero_flows and current <= 0 and previous > 0:
                continue
            delta, flow_reset = _lifetime_energy_delta(
                current_kwh=current,
                previous_kwh=previous,
                reset_drop_kwh=self._RESET_DROP_KWH,
            )
            if flow_reset:
                reset_detected = True
                break
            if delta is not None:
                signed_delta_kwh += delta * sign

        self._previous_live_flow_kwh = previous_live_flow_kwh
        self._previous_live_energy_ts = previous_live_energy_ts
        self._previous_live_sample_ts = previous_live_sample_ts
        self._previous_live_flow_sources = previous_live_flow_sources
        self._last_flow_kwh = dict(current_values)
        self._last_live_flow_sources = dict(current_sources)
        self._last_sample_ts = sample_ts

        if reset_detected:
            self._last_energy_ts = sample_ts
            self._last_power_w = 0
            self._last_method = "lifetime_reset"
            self._last_window_s = None
            self._last_reset_at = sample_ts
            self._extreme_power_validator.clear()
            return 0

        window_s = _resolve_lifetime_power_window(
            sample_ts=sample_ts,
            previous_energy_ts=self._last_energy_ts,
            default_window_s=self._DEFAULT_WINDOW_S,
        )
        minimum_window_s = self._minimum_window_seconds(flows, current_values)
        self._last_live_interval_minutes = (
            minimum_window_s / 60.0 if minimum_window_s is not None else None
        )
        if minimum_window_s is not None and window_s < minimum_window_s:
            window_s = minimum_window_s
        self._last_energy_ts = sample_ts
        self._last_window_s = window_s
        self._live_flow_sample_count += 1

        if abs(signed_delta_kwh) <= self._MIN_DELTA_KWH:
            self._last_power_w = 0
            self._last_method = "no_change"
            self._extreme_power_validator.clear()
            return 0

        candidate_power_w = _energy_delta_to_power_w(
            signed_delta_kwh,
            window_s=window_s,
        )
        extreme_validation = self._extreme_power_validator.evaluate(
            candidate_power_w, sample_ts=source_sample_ts
        )
        if not extreme_validation.accepted:
            self._last_power_w = prior_last_power_w
            self._last_method = "extreme_pending"
            return prior_last_power_w if prior_live_sample_count >= 2 else None
        if extreme_validation.confirmed_extreme:
            self._last_power_w = candidate_power_w
            self._last_method = "extreme_confirmed"
            return self._last_power_w
        if not self._power_sample_is_plausible(
            power_w=candidate_power_w,
            signed_delta_kwh=signed_delta_kwh,
            current_values=current_values,
            previous_values=previous_live_flow_kwh,
        ):
            self._last_power_w = prior_last_power_w
            self._last_method = "outlier_ignored"
            return prior_last_power_w if prior_live_sample_count >= 2 else None
        self._last_power_w = candidate_power_w
        self._last_method = "lifetime_energy_window"
        return self._last_power_w

    @property
    def extra_state_attributes(self) -> Any:
        return {
            "sampled_at_utc": self._last_report_date_iso,
            "last_flow_kwh": dict(self._last_flow_kwh),
            "last_energy_ts": self._last_energy_ts,
            "last_sample_ts": self._last_sample_ts,
            "last_power_w": self._last_power_w,
            "last_window_seconds": self._last_window_s,
            "last_reset_at": self._last_reset_at,
            "method": self._last_method,
            "source_flows": list(self._flow_signs),
        }

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        return _SiteLifetimePowerRestoreData(
            previous_live_flow_kwh=dict(self._previous_live_flow_kwh),
            previous_live_energy_ts=self._previous_live_energy_ts,
            previous_live_sample_ts=self._previous_live_sample_ts,
            last_live_interval_minutes=self._last_live_interval_minutes,
            last_live_flow_sources=dict(self._last_live_flow_sources),
            previous_live_flow_sources=dict(self._previous_live_flow_sources),
        )


class EnphaseSiteConsumptionPowerSensor(_SiteBaseEntity, RestoreEntity):  # type: ignore[misc]
    """Average site consumption from consecutive authoritative energy buckets."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True
    _attr_translation_key = "site_consumption_power"
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes | frozenset(
        {"sampled_at_utc", "last_window_seconds", "method"}
    )

    _DEFAULT_INTERVAL_MINUTES = 5.0
    _MAX_INTERVAL_FACTOR = 3.0
    _MAX_FUTURE_SKEW_SECONDS = 60.0

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "site_consumption_power",
            "Current Power Consumption",
            type_key=None,
        )
        self._last_bucket_wh: float | None = None
        self._last_bucket_count: int | None = None
        self._last_start_date: str | None = None
        self._last_energy_ts: float | None = None
        self._last_interval_minutes: float | None = None
        self._last_power_w: int | None = None
        self._last_window_s: float | None = None
        self._last_method = "seeded"
        self._restored_pending_validation = False

    @staticmethod
    def _timestamp_age_seconds(timestamp: float) -> float:
        now = dt_util.utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return float(now.timestamp()) - timestamp

    def _timestamp_is_too_far_in_future(self, timestamp: float) -> bool:
        return self._timestamp_age_seconds(timestamp) < -self._MAX_FUTURE_SKEW_SECONDS

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_extra = await self.async_get_last_extra_data()
        restored = _SiteConsumptionPowerRestoreData.from_dict(
            last_extra.as_dict() if last_extra is not None else None
        )
        if (
            restored.latest_bucket_wh is None
            or restored.latest_bucket_wh < 0
            or restored.raw_bucket_count is None
            or restored.raw_bucket_count <= 0
            or restored.energy_ts is None
            or restored.energy_ts <= 0
            or restored.interval_minutes is None
            or restored.interval_minutes <= 0
            or restored.last_power_w is None
            or restored.last_power_w < 0
        ):
            return
        if self._timestamp_is_too_far_in_future(restored.energy_ts):
            return
        self._last_bucket_wh = restored.latest_bucket_wh
        self._last_bucket_count = restored.raw_bucket_count
        self._last_start_date = restored.start_date
        self._last_energy_ts = restored.energy_ts
        self._last_interval_minutes = restored.interval_minutes
        self._last_power_w = restored.last_power_w
        self._last_window_s = restored.last_window_seconds
        self._last_method = restored.method or "restored"
        self._restored_pending_validation = True

    @staticmethod
    def _coerce_nonnegative_float(value: object) -> float | None:
        try:
            numeric = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) and numeric >= 0 else None

    @staticmethod
    def _coerce_positive_int(value: object) -> int | None:
        numeric = _restore_optional_int_value(value)
        return numeric if numeric is not None and numeric > 0 else None

    def _flow_data(self) -> dict[str, object]:
        energy = getattr(self._coord, "energy", None)
        flows = (
            getattr(energy, "site_energy", None)
            if energy is not None
            else getattr(self._coord, "site_energy", None)
        )
        if not isinstance(flows, dict):
            return {}
        entry = flows.get("consumption")
        if isinstance(entry, SiteEnergyFlow):
            return {
                "latest_bucket_wh": entry.latest_bucket_wh,
                "previous_bucket_wh": entry.previous_bucket_wh,
                "raw_bucket_count": entry.raw_bucket_count,
                "start_date": entry.start_date,
                "last_report_date": entry.last_report_date,
                "update_pending": entry.update_pending,
                "interval_minutes": entry.interval_minutes,
            }
        return entry if isinstance(entry, dict) else {}

    def _discard_baseline(self, method: str) -> None:
        self._last_bucket_wh = None
        self._last_bucket_count = None
        self._last_start_date = None
        self._last_energy_ts = None
        self._last_interval_minutes = None
        self._last_power_w = None
        self._last_window_s = None
        self._last_method = method
        self._restored_pending_validation = False

    def _seed(
        self,
        *,
        latest_bucket_wh: float,
        bucket_count: int,
        start_date: str | None,
        energy_ts: float,
        interval_minutes: float,
        method: str,
    ) -> None:
        self._last_bucket_wh = latest_bucket_wh
        self._last_bucket_count = bucket_count
        self._last_start_date = start_date
        self._last_energy_ts = energy_ts
        self._last_interval_minutes = interval_minutes
        self._last_power_w = None
        self._last_window_s = None
        self._last_method = method
        self._restored_pending_validation = False

    def _process_current_sample(self) -> None:
        data = self._flow_data()
        if not data or data.get("update_pending") is True:
            return

        latest_bucket_wh = self._coerce_nonnegative_float(data.get("latest_bucket_wh"))
        bucket_count = self._coerce_positive_int(data.get("raw_bucket_count"))
        energy_ts = _EnphaseSiteLifetimePowerSensor._parse_sample_timestamp(
            data.get("last_report_date")
        )
        interval_minutes = self._coerce_nonnegative_float(data.get("interval_minutes"))
        if interval_minutes is None or interval_minutes <= 0:
            interval_minutes = self._DEFAULT_INTERVAL_MINUTES
        start_date_raw = data.get("start_date")
        start_date = start_date_raw if isinstance(start_date_raw, str) else None

        if latest_bucket_wh is None or bucket_count is None:
            self._discard_baseline("invalid_bucket")
            return
        if energy_ts is None or energy_ts <= 0:
            return
        if self._timestamp_is_too_far_in_future(energy_ts):
            return

        if (
            self._last_bucket_wh is None
            or self._last_bucket_count is None
            or self._last_energy_ts is None
            or self._last_interval_minutes is None
        ):
            self._seed(
                latest_bucket_wh=latest_bucket_wh,
                bucket_count=bucket_count,
                start_date=start_date,
                energy_ts=energy_ts,
                interval_minutes=interval_minutes,
                method="seeded",
            )
            return

        if energy_ts == self._last_energy_ts:
            if not self._restored_pending_validation:
                return
            if (
                bucket_count == self._last_bucket_count
                and start_date == self._last_start_date
                and math.isclose(
                    latest_bucket_wh,
                    self._last_bucket_wh,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
                and math.isclose(
                    interval_minutes,
                    self._last_interval_minutes,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            ):
                self._restored_pending_validation = False
                return
            self._seed(
                latest_bucket_wh=latest_bucket_wh,
                bucket_count=bucket_count,
                start_date=start_date,
                energy_ts=energy_ts,
                interval_minutes=interval_minutes,
                method="restore_mismatch",
            )
            return

        elapsed_s = energy_ts - self._last_energy_ts
        interval_s = interval_minutes * 60.0
        if elapsed_s < 0:
            return
        if elapsed_s > interval_s * self._MAX_INTERVAL_FACTOR or not math.isclose(
            interval_minutes,
            self._last_interval_minutes,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            self._seed(
                latest_bucket_wh=latest_bucket_wh,
                bucket_count=bucket_count,
                start_date=start_date,
                energy_ts=energy_ts,
                interval_minutes=interval_minutes,
                method="interval_discontinuity",
            )
            return

        delta_wh: float | None = None
        method = "consumption_bucket_delta"
        if (
            start_date == self._last_start_date
            and bucket_count == self._last_bucket_count
        ):
            delta_wh = latest_bucket_wh - self._last_bucket_wh
        elif (
            start_date == self._last_start_date
            and bucket_count == self._last_bucket_count + 1
        ):
            previous_bucket_wh = self._coerce_nonnegative_float(
                data.get("previous_bucket_wh")
            )
            if (
                previous_bucket_wh is not None
                and previous_bucket_wh >= self._last_bucket_wh
            ):
                delta_wh = (
                    previous_bucket_wh - self._last_bucket_wh
                ) + latest_bucket_wh
                method = "consumption_bucket_rollover"

        if delta_wh is None or delta_wh < 0:
            self._seed(
                latest_bucket_wh=latest_bucket_wh,
                bucket_count=bucket_count,
                start_date=start_date,
                energy_ts=energy_ts,
                interval_minutes=interval_minutes,
                method="bucket_discontinuity",
            )
            return

        window_s = max(elapsed_s, interval_s)
        self._last_power_w = max(round(delta_wh * 3600.0 / window_s), 0)
        self._last_window_s = window_s
        self._last_method = method
        self._last_bucket_wh = latest_bucket_wh
        self._last_bucket_count = bucket_count
        self._last_start_date = start_date
        self._last_energy_ts = energy_ts
        self._last_interval_minutes = interval_minutes
        self._restored_pending_validation = False

    def _sample_is_fresh(self) -> bool:
        if self._last_energy_ts is None or self._last_interval_minutes is None:
            return False
        age_s = self._timestamp_age_seconds(self._last_energy_ts)
        return bool(
            -self._MAX_FUTURE_SKEW_SECONDS
            <= age_s
            <= (self._last_interval_minutes * 60.0 * self._MAX_INTERVAL_FACTOR)
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        self._process_current_sample()
        return bool(
            self._last_power_w is not None
            and not self._restored_pending_validation
            and self._sample_is_fresh()
        )

    @property
    def native_value(self) -> int | None:
        self._process_current_sample()
        return self._last_power_w

    @property
    def extra_state_attributes(self) -> Any:
        sampled_at = None
        if self._last_energy_ts is not None:
            sampled_at = datetime.fromtimestamp(
                self._last_energy_ts, tz=timezone.utc
            ).isoformat()
        return {
            "sampled_at_utc": sampled_at,
            "last_window_seconds": self._last_window_s,
            "method": self._last_method,
        }

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        return _SiteConsumptionPowerRestoreData(
            latest_bucket_wh=self._last_bucket_wh,
            raw_bucket_count=self._last_bucket_count,
            start_date=self._last_start_date,
            energy_ts=self._last_energy_ts,
            interval_minutes=self._last_interval_minutes,
            last_power_w=self._last_power_w,
            last_window_seconds=self._last_window_s,
            method=self._last_method,
        )


class EnphaseGridPowerSensor(_EnphaseSiteLifetimePowerSensor):
    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "grid_power",
            "Current Grid Power",
            translation_key="site_grid_power",
            flow_signs={"grid_import": 1, "grid_export": -1},
            type_key=None,
        )


class EnphaseBatteryPowerSensor(_EnphaseSiteLifetimePowerSensor):
    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "battery_power",
            "Current Battery Power",
            translation_key="site_battery_power",
            flow_signs={"battery_discharge": 1, "battery_charge": -1},
            type_key=None,
        )


class EnphaseCurrentPowerConsumptionSensor(_SiteBaseEntity, RestoreSensor):  # type: ignore[misc]
    _attr_translation_key = "current_production_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord,
            "current_production_power",
            "Current Production Power",
            type_key=None,
        )
        self._last_good_value: float | None = None
        self._last_good_sample_utc: datetime | None = None
        self._last_good_cached_at_utc: datetime | None = None
        self._last_good_source: str | None = None
        self._last_good_reported_units: str | None = None
        self._last_good_reported_precision: int | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None:
            try:
                restored = (
                    float(last.native_value) if last.native_value is not None else None
                )
            except Exception:  # noqa: BLE001
                restored = None
            if (
                restored is not None
                and math.isfinite(restored)
                and abs(restored) < EXTREME_SITE_POWER_W
            ):
                self._last_good_value = restored

        try:
            last_state = await self.async_get_last_state()
        except Exception:  # noqa: BLE001
            last_state = None
        if last_state is None:
            return
        attrs = last_state.attributes or {}
        sample_raw = attrs.get("sampled_at_utc")
        if isinstance(sample_raw, str):
            parsed = dt_util.parse_datetime(sample_raw)
            if parsed is not None:
                self._last_good_sample_utc = _normalize_utc_datetime(parsed)
        cached_raw = attrs.get("cached_at_utc")
        if isinstance(cached_raw, str):
            parsed_cached = dt_util.parse_datetime(cached_raw)
            if parsed_cached is not None:
                self._last_good_cached_at_utc = _normalize_utc_datetime(parsed_cached)
        source = attrs.get("source")
        if isinstance(source, str) and source.strip():
            self._last_good_source = source
        units = attrs.get("reported_units")
        if isinstance(units, str) and units.strip():
            self._last_good_reported_units = units
        precision = attrs.get("reported_precision")
        try:
            if precision is not None:
                self._last_good_reported_precision = int(precision)
        except Exception:  # noqa: BLE001
            self._last_good_reported_precision = None

    def _cache_ttl(self) -> timedelta:
        interval = getattr(self._coord, "update_interval", None)
        if isinstance(interval, timedelta) and interval.total_seconds() > 0:
            return interval * CURRENT_POWER_CACHE_TTL_MULTIPLIER
        return timedelta(minutes=CURRENT_POWER_CACHE_TTL_MULTIPLIER)

    def _freshness_reference_utc(self) -> datetime:
        success_utc = _normalize_utc_datetime(
            getattr(self._coord, "last_success_utc", None)
        )
        try:
            now = _normalize_utc_datetime(dt_util.utcnow())
        except Exception:  # noqa: BLE001
            now = None
        if success_utc is not None and now is not None:
            return max(success_utc, now)
        if success_utc is not None:
            return success_utc
        if now is not None:
            return now
        return datetime.now(timezone.utc)

    def _cached_sample_is_fresh(self) -> bool:
        sample_utc = self._last_good_cached_at_utc or self._last_good_sample_utc
        if sample_utc is None:
            return False
        reference_utc = self._freshness_reference_utc()
        return reference_utc - sample_utc <= self._cache_ttl()

    def _clear_last_good_sample(self) -> None:
        self._last_good_value = None
        self._last_good_sample_utc = None
        self._last_good_cached_at_utc = None
        self._last_good_source = None
        self._last_good_reported_units = None
        self._last_good_reported_precision = None

    def _current_or_cached_snapshot(
        self,
    ) -> tuple[float | None, datetime | None, str | None, str | None, int | None]:
        value = self._coord.current_power_consumption_w
        sample_utc = self._coord.current_power_consumption_sample_utc
        source = self._coord.current_power_consumption_source
        units = self._coord.current_power_consumption_reported_units
        precision = self._coord.current_power_consumption_reported_precision

        if value is not None:
            self._last_good_value = float(value)
            self._last_good_sample_utc = _normalize_utc_datetime(sample_utc)
            self._last_good_cached_at_utc = (
                self._last_good_sample_utc
                or _normalize_utc_datetime(
                    getattr(self._coord, "last_success_utc", None)
                )
                or self._freshness_reference_utc()
            )
            self._last_good_source = source
            self._last_good_reported_units = units
            self._last_good_reported_precision = precision
            return (
                float(value),
                self._last_good_sample_utc,
                source,
                units,
                precision,
            )

        if self._last_good_value is not None and not self._cached_sample_is_fresh():
            self._clear_last_good_sample()

        return (
            self._last_good_value,
            self._last_good_sample_utc,
            self._last_good_source,
            self._last_good_reported_units,
            self._last_good_reported_precision,
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        value, _sample_utc, _source, _units, _precision = (
            self._current_or_cached_snapshot()
        )
        return value is not None

    @property
    def native_value(self) -> Any:
        value, _sample_utc, _source, _units, _precision = (
            self._current_or_cached_snapshot()
        )
        if value is None:
            return None
        rounded = round(value, 3)
        if float(rounded).is_integer():
            return int(rounded)
        return rounded

    @property
    def extra_state_attributes(self) -> Any:
        _value, sample_utc, source, units, precision = (
            self._current_or_cached_snapshot()
        )
        return {
            "sampled_at_utc": (
                sample_utc.isoformat() if sample_utc is not None else None
            ),
            "cached_at_utc": (
                self._last_good_cached_at_utc.isoformat()
                if self._last_good_cached_at_utc is not None
                else None
            ),
            "source": source,
            "reported_units": units,
            "reported_precision": precision,
            "using_stale": bool(self._coord.current_power_runtime.using_stale),
        }

    @property
    def device_info(self) -> Any:
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)
