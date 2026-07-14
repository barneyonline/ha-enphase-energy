"""Shared base classes for Enphase sensor feature modules."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
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
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_{key}"

    @property
    def available(self) -> bool:
        """Return whether the site sensor is available."""

        if self._type_key is not None and not inventory_type_available(
            self._coord, self._type_key
        ):
            return False
        if self._coord.last_success_utc is not None:
            return True
        return super().available  # type: ignore[no-any-return]

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
