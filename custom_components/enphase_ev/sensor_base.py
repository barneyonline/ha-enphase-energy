"""Shared base classes for Enphase sensor feature modules."""

from __future__ import annotations

from typing import Any
from datetime import datetime, timedelta
from collections.abc import Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.event import async_track_point_in_utc_time
from .entity import callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .device_info_helpers import _cloud_device_info
from .runtime_helpers import (
    inventory_type_available,
    inventory_type_device_info,
)


class EnphaseSiteSensorEntity(CoordinatorEntity, SensorEntity):  # type: ignore[misc]
    """Base entity for sensors associated with site-level equipment."""

    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset(
        {
            "last_success_utc",
            "last_failure_utc",
            "backoff_ends_utc",
            "last_failure_response",
        }
    )

    def __init__(
        self,
        coord: EnphaseCoordinator,
        key: str,
        _name: str,
        type_key: str | None = "envoy",
    ) -> None:
        super().__init__(coord)
        self._coord = coord
        self._key = key
        self._type_key = type_key
        self._cancel_freshness_expiry: Callable[[], None] | None = None
        self._source_first_observed: datetime | None = None
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{key}"

    @property
    def available(self) -> bool:
        """Return whether the site sensor is available."""

        if self._type_key is not None and not inventory_type_available(
            self._coord, self._type_key
        ):
            return False
        deadline = self._freshness_deadline()
        if deadline is not None and dt_util.utcnow() >= deadline:
            return False
        if self._coord.last_success_utc is not None:
            return True
        return super().available  # type: ignore[no-any-return]

    def _freshness_deadline(self) -> datetime | None:
        """Bound live readings while preserving cumulative and diagnostic state."""

        if self.device_class not in {
            SensorDeviceClass.POWER,
            SensorDeviceClass.BATTERY,
            SensorDeviceClass.ENERGY_STORAGE,
        }:
            return None
        source_success: datetime | None = None
        family_source = self._type_key in {"heatpump", "encharge", "ac_battery"}
        if self._type_key == "heatpump":
            source_success = getattr(
                self._coord, "heatpump_power_last_success_utc", None
            )
        elif self._type_key in {"encharge", "ac_battery"}:
            getter = getattr(self._coord, "endpoint_family_last_success_utc", None)
            if callable(getter):
                source_success = getter(
                    "battery_status"
                    if self._type_key == "encharge"
                    else "ac_battery_telemetry"
                )
        if family_source and not isinstance(source_success, datetime):
            if self._source_first_observed is None:
                self._source_first_observed = dt_util.utcnow()
            source_success = self._source_first_observed
        if not isinstance(source_success, datetime):
            source_success = (
                self._coord.last_success_utc
                if not self._coord.last_update_success
                else None
            )
        return (
            source_success + timedelta(minutes=30 if family_source else 15)
            if source_success is not None
            else None
        )

    @callback
    def _schedule_freshness_expiry(self) -> None:
        if self._cancel_freshness_expiry is not None:
            self._cancel_freshness_expiry()
            self._cancel_freshness_expiry = None
        deadline = self._freshness_deadline()
        if self.hass is None or deadline is None or deadline <= dt_util.utcnow():
            return

        @callback
        def expire(_now: datetime) -> None:
            self._cancel_freshness_expiry = None
            # Successful unchanged polls may advance source freshness without
            # notifying listeners; follow their new deadline before publishing.
            self._schedule_freshness_expiry()
            self.async_write_ha_state()

        self._cancel_freshness_expiry = async_track_point_in_utc_time(
            self.hass, expire, deadline
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_freshness_expiry()

    async def async_will_remove_from_hass(self) -> None:
        if self._cancel_freshness_expiry is not None:
            self._cancel_freshness_expiry()
            self._cancel_freshness_expiry = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_freshness_expiry()
        super()._handle_coordinator_update()

    def _cloud_diag_attrs(
        self, *, include_last_success: bool = True
    ) -> dict[str, object]:
        """Return sanitized cloud diagnostic attributes."""

        attrs: dict[str, object] = {}
        if include_last_success and self._coord.last_success_utc:
            attrs["last_success_utc"] = self._coord.last_success_utc.isoformat()
        if self._coord.last_failure_utc:
            attrs["last_failure_utc"] = self._coord.last_failure_utc.isoformat()
        if self._coord.last_failure_status is not None:
            attrs["last_failure_status"] = self._coord.last_failure_status
        if self._coord.last_failure_description:
            attrs["code_description"] = self._coord.last_failure_description
        if self._coord.last_failure_response:
            attrs["last_failure_response"] = self._coord.last_failure_response
        if self._coord.last_failure_source:
            attrs["last_failure_source"] = self._coord.last_failure_source
        if last_failure_endpoint := getattr(self._coord, "last_failure_endpoint", None):
            attrs["last_failure_endpoint"] = last_failure_endpoint
        if payload_failure_kind := getattr(self._coord, "payload_failure_kind", None):
            attrs["payload_failure_kind"] = payload_failure_kind
        if bool(getattr(self._coord, "payload_using_stale", False)):
            attrs["payload_using_stale"] = True
        if self._coord.backoff_ends_utc:
            attrs["backoff_ends_utc"] = self._coord.backoff_ends_utc.isoformat()
        return attrs

    def _backoff_remaining_seconds(self) -> int | None:
        """Return seconds remaining in the coordinator backoff window."""

        ends = self._coord.backoff_ends_utc
        if ends is None:
            return None
        try:
            remaining = (ends - dt_util.utcnow()).total_seconds()
        except Exception:
            return None
        if remaining <= 0:
            return 0
        rounded = int(round(remaining))
        return rounded if rounded > 0 else 1

    @property
    def extra_state_attributes(self) -> Any:
        """Return cloud diagnostic state attributes."""

        return self._cloud_diag_attrs()

    @property
    def device_info(self) -> Any:
        """Return device information for the site equipment family."""

        if self._type_key is None:
            return _cloud_device_info(self._coord.site_id)
        if info := inventory_type_device_info(self._coord, self._type_key):
            return info

        from homeassistant.helpers.entity import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, f"type:{self._coord.site_id}:{self._type_key}")},
            manufacturer="Enphase",
        )


# Compatibility alias for existing feature modules and third-party imports.
_SiteBaseEntity = EnphaseSiteSensorEntity
