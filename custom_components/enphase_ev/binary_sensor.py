from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .device_info_helpers import _cloud_device_info
from .entity import EnphaseBaseEntity
from .entity_cleanup import prune_managed_entities
from .runtime_helpers import (
    inventory_type_available as _type_available,
    inventory_type_device_info as _type_device_info,
)
from .runtime_data import EnphaseConfigEntry, get_runtime_data
from .serial_discovery import active_charger_serials_for_cleanup
from .serial_entity_metadata import (
    CHARGER_BINARY_SENSOR_UNIQUE_SUFFIXES,
    HISTORICAL_CHARGER_BINARY_SENSOR_UNIQUE_SUFFIXES,
    charger_entity_unique_ids,
)
from .sensor import (
    _heatpump_runtime_device_uid,
    _heatpump_runtime_snapshot,
)

PARALLEL_UPDATES = 0


def _charger_binary_sensor_unique_ids(serials: set[str]) -> set[str]:
    return {
        unique_id
        for serial in serials
        for unique_id in charger_entity_unique_ids(
            serial, CHARGER_BINARY_SENSOR_UNIQUE_SUFFIXES
        )
    }


def _is_managed_charger_binary_sensor(unique_id: str) -> bool:
    site_prefix = f"{DOMAIN}_site_"
    if unique_id.startswith(site_prefix):
        return False
    return unique_id.startswith(f"{DOMAIN}_") and unique_id.endswith(
        CHARGER_BINARY_SENSOR_UNIQUE_SUFFIXES
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    coord: EnphaseCoordinator = get_runtime_data(entry).coordinator
    ent_reg = er.async_get(hass)
    site_entity_added = False
    heatpump_sg_ready_entity_added = False
    known_serials: set[str] = set()

    @callback
    def _async_prune_historical_charger_binary_sensor_entities() -> None:
        entities = getattr(ent_reg, "entities", None)
        if not isinstance(entities, dict):
            return
        unique_prefix = f"{DOMAIN}_"
        for reg_entry in list(entities.values()):
            entry_domain = getattr(reg_entry, "domain", None)
            if entry_domain is None:
                entry_domain = reg_entry.entity_id.partition(".")[0]
            if entry_domain != "binary_sensor":
                continue
            entry_platform = getattr(reg_entry, "platform", None)
            if entry_platform is not None and entry_platform != DOMAIN:
                continue
            entry_config_id = getattr(reg_entry, "config_entry_id", None)
            if entry_config_id is not None and entry_config_id != entry.entry_id:
                continue
            unique_id = getattr(reg_entry, "unique_id", None)
            if not isinstance(unique_id, str) or not unique_id.startswith(
                unique_prefix
            ):
                continue
            if not unique_id.endswith(HISTORICAL_CHARGER_BINARY_SENSOR_UNIQUE_SUFFIXES):
                continue
            ent_reg.async_remove(reg_entry.entity_id)

    def _site_binary_sensor_unique_id(key: str) -> str:
        return f"{DOMAIN}_site_{coord.site_id}_{key}"

    @callback
    def _async_remove_site_binary_entity(key: str) -> None:
        nonlocal heatpump_sg_ready_entity_added
        entity_id = ent_reg.async_get_entity_id(
            "binary_sensor",
            DOMAIN,
            _site_binary_sensor_unique_id(key),
        )
        if entity_id is not None:
            ent_reg.async_remove(entity_id)
        if key == "heat_pump_sg_ready_active":
            heatpump_sg_ready_entity_added = False

    @callback
    def _async_sync_chargers() -> None:
        nonlocal site_entity_added, heatpump_sg_ready_entity_added
        inventory_ready = bool(getattr(coord, "_devices_inventory_ready", False))
        if not site_entity_added:
            async_add_entities(
                [SiteCloudReachableBinarySensor(coord)], update_before_add=False
            )
            site_entity_added = True
        heatpump_runtime_available = _heatpump_runtime_device_uid(coord) is not None
        if heatpump_runtime_available and not heatpump_sg_ready_entity_added:
            async_add_entities(
                [HeatPumpSgReadyActiveBinarySensor(coord)],
                update_before_add=False,
            )
            heatpump_sg_ready_entity_added = True
        elif inventory_ready and not heatpump_runtime_available:
            _async_remove_site_binary_entity("heat_pump_sg_ready_active")
        active_charger_serials = active_charger_serials_for_cleanup(coord)
        if active_charger_serials is not None:
            prune_managed_entities(
                ent_reg,
                entry.entry_id,
                domain="binary_sensor",
                active_unique_ids=_charger_binary_sensor_unique_ids(
                    active_charger_serials
                ),
                is_managed=_is_managed_charger_binary_sensor,
            )
            known_serials.intersection_update(active_charger_serials)
        serials = [sn for sn in coord.iter_serials() if sn and sn not in known_serials]
        if not serials:
            return
        entities = []
        for sn in serials:
            entities.append(PluggedInBinarySensor(coord, sn))
            entities.append(ChargingBinarySensor(coord, sn))
            entities.append(ConnectedBinarySensor(coord, sn))
        if entities:
            async_add_entities(entities, update_before_add=False)
            known_serials.update(serials)

    add_listener = getattr(coord, "async_add_topology_listener", None)
    if not callable(add_listener):
        add_listener = getattr(coord, "async_add_listener", None)
    if callable(add_listener):
        entry.async_on_unload(add_listener(_async_sync_chargers))
    _async_prune_historical_charger_binary_sensor_entities()
    _async_sync_chargers()


class _EVBoolSensor(EnphaseBaseEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _translation_key: str | None = None

    def __init__(self, coord: EnphaseCoordinator, sn: str, key: str, tkey: str):
        super().__init__(coord, sn)
        self._key = key
        self._attr_unique_id = f"{DOMAIN}_{sn}_{key}"
        self._attr_translation_key = tkey

    @property
    def is_on(self) -> bool:
        v = self.data.get(self._key)
        return bool(v)

    # available and device_info inherited from base


class PluggedInBinarySensor(_EVBoolSensor):
    _attr_device_class = BinarySensorDeviceClass.PLUG

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "plugged", "plugged_in")


class ChargingBinarySensor(_EVBoolSensor):
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "charging", "charging")

    @property
    def icon(self) -> str | None:
        # Lightning bolt when charging, dimmed/off otherwise
        return "mdi:flash" if self.is_on else "mdi:flash-off"


class ConnectedBinarySensor(_EVBoolSensor):
    def __init__(self, coord: EnphaseCoordinator, sn: str):
        super().__init__(coord, sn, "connected", "connected")
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def extra_state_attributes(self):
        connection = self.data.get("connection")
        if isinstance(connection, str):
            connection = connection.strip() or None
        ip_attr = self.data.get("ip_address")
        if isinstance(ip_attr, str):
            ip_attr = ip_attr.strip() or None
        return {
            "connection": connection,
            "ip_address": ip_attr,
        }


class SiteCloudReachableBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "cloud_reachable"

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = f"{DOMAIN}_site_{coord.site_id}_cloud_reachable"

    @property
    def available(self) -> bool:
        if self._coord.last_success_utc is not None:
            return True
        return super().available

    @property
    def is_on(self) -> bool:
        last = self._coord.last_success_utc
        if not last:
            return False
        now = dt_util.utcnow()
        interval = (
            self._coord.update_interval.total_seconds()
            if self._coord.update_interval
            else 30
        )
        threshold = interval * 2
        return (now - last).total_seconds() <= threshold

    @property
    def extra_state_attributes(self):
        return {}

    @property
    def device_info(self):
        info = _type_device_info(self._coord, "cloud")
        if info is not None:
            return info
        return _cloud_device_info(self._coord.site_id)


class HeatPumpSgReadyActiveBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "heat_pump_sg_ready_active"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: EnphaseCoordinator):
        super().__init__(coord)
        self._coord = coord
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coord.site_id}_heat_pump_sg_ready_active"
        )

    def _snapshot(self) -> dict[str, object]:
        return _heatpump_runtime_snapshot(self._coord)

    @property
    def available(self) -> bool:
        if not _type_available(self._coord, "heatpump"):
            return False
        runtime_uid_getter = getattr(self._coord, "_heatpump_runtime_device_uid", None)
        if callable(runtime_uid_getter):
            try:
                if not runtime_uid_getter():
                    return False
            except Exception:  # noqa: BLE001
                return False
        snapshot = self._snapshot()
        return any(
            snapshot.get(key) is not None
            for key in ("sg_ready_active", "sg_ready_mode_raw", "sg_ready_mode_label")
        )

    @property
    def is_on(self) -> bool:
        return bool(self._snapshot().get("sg_ready_active"))

    @property
    def extra_state_attributes(self):
        return {}

    @property
    def device_info(self):
        return _type_device_info(self._coord, "heatpump")
