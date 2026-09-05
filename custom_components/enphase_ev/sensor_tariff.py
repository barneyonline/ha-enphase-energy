"""Tariff and billing sensor entities."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .coordinator import EnphaseCoordinator
from .device_info_helpers import _cloud_device_info
from .runtime_helpers import (
    coerce_optional_text as _gateway_clean_text,
)
from .runtime_helpers import (
    inventory_type_device_info as _type_device_info,
)
from .sensor_base import EnphaseSiteSensorEntity as _SiteBaseEntity
from .sensor_common import callback
from .tariff import (
    current_tariff_rate_sensor_spec,
    next_billing_date,
    next_tariff_rate_change,
    tariff_rate_sensor_specs,
)


def _tariff_data_available(coord: EnphaseCoordinator) -> bool:
    return any(
        getattr(coord, attr, None) is not None
        for attr in (
            "tariff_billing",
            "tariff_import_rate",
            "tariff_export_rate",
        )
    )


def _tariff_now(coord: EnphaseCoordinator, hass: HomeAssistant | None) -> datetime:
    tz_name = None
    site_tz = getattr(coord, "_site_timezone_name", None)
    if callable(site_tz):
        tz_name = site_tz()
    if not isinstance(tz_name, str) or not tz_name.strip():
        tz_name = getattr(getattr(hass, "config", None), "time_zone", None)
    tzinfo = dt_util.get_time_zone(tz_name) if isinstance(tz_name, str) else None
    return dt_util.now(tzinfo or dt_util.DEFAULT_TIME_ZONE)  # type: ignore[no-any-return]


class _EnphaseTariffBaseSensor(_SiteBaseEntity):
    _unrecorded_attributes = _SiteBaseEntity._unrecorded_attributes.union(
        {
            "configured_rates",
            "seasons",
            "last_refresh_utc",
        }
    )

    @property
    def available(self) -> bool:
        return bool(_tariff_data_available(self._coord) and super().available)

    @property
    def device_info(self) -> Any:
        info = _type_device_info(self._coord, "envoy")
        if info is not None:
            return info
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)

    def _last_refresh_attr(self) -> dict[str, object]:
        last_refresh = getattr(self._coord, "tariff_last_refresh_utc", None)
        if isinstance(last_refresh, datetime):
            return {"last_refresh_utc": last_refresh.isoformat()}
        return {}


class EnphaseTariffBillingSensor(_EnphaseTariffBaseSensor):
    _attr_translation_key = "tariff_billing_cycle"
    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:calendar-month"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(
            coord, "tariff_billing_cycle", "Next Billing Date", type_key=None
        )

    def _snapshot(self) -> Any:
        return getattr(self._coord, "tariff_billing", None)

    @property
    def available(self) -> bool:
        snapshot = self._snapshot()
        return (
            snapshot is not None
            and next_billing_date(snapshot) is not None
            and super().available
        )

    @property
    def native_value(self) -> Any:
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        return next_billing_date(snapshot)

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._snapshot()
        if snapshot is None:
            return {}
        attrs = dict(snapshot.attributes)
        attrs.update(self._last_refresh_attr())
        return attrs


class EnphaseTariffRateSensor(_EnphaseTariffBaseSensor):
    def __init__(self, coord: EnphaseCoordinator, is_import: bool) -> None:
        self._is_import = is_import
        key = "tariff_import_rate" if is_import else "tariff_export_rate"
        name = "Import Rate" if is_import else "Export Rate"
        self._attr_translation_key = key
        self._attr_icon = "mdi:cash-minus" if is_import else "mdi:cash-plus"
        super().__init__(coord, key, name, type_key=None)

    def _snapshot(self) -> Any:
        attr = "tariff_import_rate" if self._is_import else "tariff_export_rate"
        return getattr(self._coord, attr, None)

    @property
    def available(self) -> bool:
        return self._snapshot() is not None and super().available

    @property
    def native_value(self) -> Any:
        snapshot = self._snapshot()
        return getattr(snapshot, "state", None)

    @property
    def extra_state_attributes(self) -> Any:
        snapshot = self._snapshot()
        if snapshot is None:
            return {}
        attrs = dict(snapshot.attributes)
        attrs.update(self._last_refresh_attr())
        return attrs


class EnphaseCurrentTariffRateSensor(_EnphaseTariffBaseSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(self, coord: EnphaseCoordinator, *, is_import: bool) -> None:
        self._is_import = is_import
        key = (
            "tariff_current_import_rate" if is_import else "tariff_current_export_rate"
        )
        name = "Current Import Rate" if is_import else "Current Export Rate"
        self._rate_attr = "tariff_import_rate" if is_import else "tariff_export_rate"
        self._attr_translation_key = key
        self._attr_icon = "mdi:cash-minus" if is_import else "mdi:cash-plus"
        self._tariff_boundary_cancel: CALLBACK_TYPE | None = None
        super().__init__(coord, key, name, type_key=None)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._ensure_tariff_boundary_timer()

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        self._cancel_tariff_boundary_timer()

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self._ensure_tariff_boundary_timer()

    def _spec(self) -> dict[str, object] | None:
        return current_tariff_rate_sensor_spec(
            getattr(self._coord, self._rate_attr, None),
            _tariff_now(self._coord, getattr(self, "hass", None)),
        )

    def _configured_rates(self) -> list[dict[str, object]]:
        rates: list[dict[str, object]] = []
        for spec in tariff_rate_sensor_specs(
            getattr(self._coord, self._rate_attr, None)
        ):
            raw_attrs = spec.get("attributes")
            attrs = raw_attrs if isinstance(raw_attrs, dict) else {}
            rate: dict[str, object] = {
                key: value
                for key, value in {
                    "name": spec.get("name"),
                    "rate": attrs.get("rate"),
                    "formatted_rate": attrs.get("formatted_rate"),
                    "unit": spec.get("unit"),
                    "season_id": attrs.get("season_id"),
                    "start_month": attrs.get("start_month"),
                    "end_month": attrs.get("end_month"),
                    "day_group_id": attrs.get("day_group_id"),
                    "days": attrs.get("days"),
                    "period_type": attrs.get("period_type"),
                    "start_time": attrs.get("start_time"),
                    "end_time": attrs.get("end_time"),
                    "tier_id": attrs.get("tier_id"),
                    "start_value": attrs.get("start_value"),
                    "end_value": attrs.get("end_value"),
                    "unbounded": attrs.get("unbounded"),
                    "tariff_locator": attrs.get("tariff_locator"),
                }.items()
                if value is not None
            }
            rates.append(rate)
        return rates

    @property
    def available(self) -> bool:
        return self._spec() is not None and super().available

    @property
    def native_value(self) -> Any:
        spec = self._spec()
        if spec is None:
            return None
        return spec.get("state")

    @property
    def native_unit_of_measurement(self) -> Any:
        spec = self._spec()
        if spec is None:
            return None
        hass = getattr(self, "hass", None)
        currency = _gateway_clean_text(
            getattr(getattr(hass, "config", None), "currency", None)
        )
        if currency is not None:
            return f"{currency}/{UnitOfEnergy.KILO_WATT_HOUR}"
        return spec.get("unit")

    @property
    def extra_state_attributes(self) -> Any:
        spec = self._spec()
        if spec is None:
            return {}
        raw_attrs = spec.get("attributes")
        attrs = dict(raw_attrs) if isinstance(raw_attrs, dict) else {}
        attrs["active_rate_name"] = spec.get("name")
        attrs["configured_rates"] = self._configured_rates()
        attrs.update(self._last_refresh_attr())
        return attrs

    @callback
    def _ensure_tariff_boundary_timer(self) -> None:
        if self.hass is None:
            return
        when = _tariff_now(self._coord, self.hass)
        next_change = next_tariff_rate_change(
            getattr(self._coord, self._rate_attr, None),
            when,
        )
        self._cancel_tariff_boundary_timer()
        if next_change is None:
            return
        fire_at = dt_util.as_utc(next_change)
        if fire_at <= dt_util.utcnow():
            fire_at = dt_util.utcnow() + timedelta(seconds=1)
        self._tariff_boundary_cancel = async_track_point_in_utc_time(
            self.hass, self._handle_tariff_boundary, fire_at
        )

    @callback
    def _handle_tariff_boundary(self, _now: datetime) -> None:
        self._cancel_tariff_boundary_timer()
        self.async_write_ha_state()
        self._ensure_tariff_boundary_timer()

    @callback
    def _cancel_tariff_boundary_timer(self) -> None:
        if self._tariff_boundary_cancel:
            try:
                self._tariff_boundary_cancel()
            except Exception:  # noqa: BLE001
                pass
            self._tariff_boundary_cancel = None


class EnphaseTariffRateValueSensor(_EnphaseTariffBaseSensor):
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(
        self, coord: EnphaseCoordinator, spec: dict[str, Any], *, is_import: bool
    ) -> None:
        self._is_import = is_import
        self._rate_prefix = "tariff_import_rate" if is_import else "tariff_export_rate"
        self._rate_attr = "tariff_import_rate" if is_import else "tariff_export_rate"
        label_prefix = "Import Rate" if is_import else "Export Rate"
        self._attr_icon = "mdi:cash-minus" if is_import else "mdi:cash-plus"

        self._detail_key = str(spec.get("key") or "rate")
        detail_name = str(
            spec.get("name") or self._detail_key.replace("_", " ").title()
        )
        name = f"{label_prefix} {detail_name}"
        self._attr_translation_key = f"{self._rate_prefix}_value"
        self._attr_translation_placeholders = {"detail": detail_name}
        super().__init__(
            coord,
            f"{self._rate_prefix}_{self._detail_key}",
            name,
            type_key=None,
        )

    def _spec(self) -> Any:
        for spec in tariff_rate_sensor_specs(
            getattr(self._coord, self._rate_attr, None)
        ):
            if spec.get("key") == self._detail_key:
                return spec
        return None

    @property
    def available(self) -> bool:
        return self._spec() is not None and super().available

    @property
    def native_value(self) -> Any:
        spec = self._spec()
        if spec is None:
            return None
        return spec.get("state")

    @property
    def native_unit_of_measurement(self) -> Any:
        spec = self._spec()
        if spec is None:
            return None
        hass = getattr(self, "hass", None)
        currency = _gateway_clean_text(
            getattr(getattr(hass, "config", None), "currency", None)
        )
        if currency is not None:
            return f"{currency}/{UnitOfEnergy.KILO_WATT_HOUR}"
        return spec.get("unit")

    @property
    def extra_state_attributes(self) -> Any:
        spec = self._spec()
        if spec is None:
            return {}
        attrs = dict(spec.get("attributes") or {})
        attrs.update(self._last_refresh_attr())
        return attrs


class EnphaseTariffExportRateValueSensor(EnphaseTariffRateValueSensor):
    def __init__(self, coord: EnphaseCoordinator, spec: dict[str, Any]) -> None:
        super().__init__(coord, spec, is_import=False)
