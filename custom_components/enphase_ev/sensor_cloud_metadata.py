"""Site context and account access diagnostics for the cloud service."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory

from .cloud_metadata import ACCESS_PRIORITY
from .coordinator import EnphaseCoordinator
from .device_info_helpers import _cloud_device_info
from .sensor_base import EnphaseSiteSensorEntity


class _CloudMetadataSensor(EnphaseSiteSensorEntity):
    @property
    def device_info(self) -> Any:
        return _cloud_device_info(self._coord.site_id)


class EnphaseSiteInformationSensor(_CloudMetadataSensor):
    """Identify the site without misusing device registry fields."""

    _attr_translation_key = "site_information"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:information-outline"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "site_information", "Site information", type_key=None)

    @property
    def native_value(self) -> str:
        return str(self._coord.site_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        metadata = getattr(
            getattr(self._coord, "inventory_state", None), "cloud_metadata", {}
        )
        return {
            "site_id": str(self._coord.site_id),
            **{key: metadata.get(key) for key in ("timezone", "country", "currency")},
        }


class EnphaseAccountAccessSensor(_CloudMetadataSensor):
    """Summarize observed account roles; this is not a permission gate."""

    _attr_translation_key = "account_access"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [*ACCESS_PRIORITY, "viewer"]
    _attr_icon = "mdi:account-key-outline"

    def __init__(self, coord: EnphaseCoordinator) -> None:
        super().__init__(coord, "account_access", "Account access", type_key=None)

    @property
    def _roles(self) -> dict[str, bool]:
        metadata = getattr(
            getattr(self._coord, "inventory_state", None), "cloud_metadata", {}
        )
        roles = metadata.get("access")
        return roles if isinstance(roles, dict) else {}

    @property
    def available(self) -> bool:
        return bool(self._roles) and super().available

    @property
    def native_value(self) -> str | None:
        roles = self._roles
        if not roles:
            return None
        return next((role for role in ACCESS_PRIORITY if roles[role]), "viewer")

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        roles = self._roles
        if not roles:
            return {}
        return {**roles, "viewer": not any(roles.values())}
