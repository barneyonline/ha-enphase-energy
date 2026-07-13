"""Entity registry helpers for Enphase sensor platform setup."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, cast

from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .device_types import is_dry_contact_type_key
from .runtime_helpers import coerce_optional_text as _clean_text
from .serial_entity_metadata import (
    AC_BATTERY_ENTITY_UNIQUE_SUFFIXES,
    AC_BATTERY_RETIRED_UNIQUE_SUFFIXES,
    BATTERY_ENTITY_UNIQUE_SUFFIXES,
    BATTERY_RETIRED_UNIQUE_SUFFIXES,
    CHARGER_SENSOR_UNIQUE_SUFFIXES,
    HISTORICAL_CHARGER_SENSOR_UNIQUE_SUFFIXES,
    INVERTER_ENTITY_UNIQUE_SUFFIXES,
    ac_battery_entity_serial_from_unique_id,
    battery_entity_serial_from_unique_id,
    charger_entity_serial_from_unique_id,
    charger_entity_unique_id,
    charger_entity_unique_ids,
    inverter_entity_unique_id,
    inverter_entity_serial_from_unique_id,
    site_ac_battery_entity_unique_id,
    site_ac_battery_entity_unique_ids,
    site_battery_entity_unique_id,
    site_battery_entity_unique_ids,
)


class EnphaseSensorRegistrySetup:
    """Manage entity registry cleanup used by sensor setup."""

    def __init__(self, ent_reg: Any, *, config_entry_id: str, site_id: str) -> None:
        """Initialize the helper for one config entry."""

        self._ent_reg = ent_reg
        self._config_entry_id = config_entry_id
        self._site_id = site_id
        self.known_site_entity_keys: set[str] = set()
        self.known_type_keys: set[str] = set()
        self.known_gateway_iq_router_keys: set[str] = set()
        self.known_charger_serials: set[str] = set()
        self.known_battery_serials: set[str] = set()
        self.known_ac_battery_serials: set[str] = set()
        self.known_inverter_serials: set[str] = set()
        self.known_inverter_telemetry_serials: set[str] = set()
        self.battery_registry_pruned = False
        self.ac_battery_registry_pruned = False
        self.inverter_registry_pruned = False

    def site_sensor_unique_id(self, key: str) -> str:
        """Return the unique ID for a site-level sensor key."""

        return f"{DOMAIN}_site_{self._site_id}_{key}"

    def type_sensor_unique_id(self, type_key: str) -> str:
        """Return the unique ID for a type inventory sensor."""

        return f"{DOMAIN}_site_{self._site_id}_type_{type_key}_inventory"

    @staticmethod
    def gateway_iq_router_entity_key(router_key: str) -> str:
        """Return the site entity key for an IQ Energy Router sensor."""

        return f"gateway_iq_energy_router_{router_key}"

    def battery_sensor_unique_id(self, serial: str, suffix: str) -> str:
        """Return the unique ID for a per-storage-battery sensor."""

        return site_battery_entity_unique_id(self._site_id, serial, suffix)

    def battery_sensor_unique_ids(self, serial: str) -> tuple[str, ...]:
        """Return active unique IDs for a per-storage-battery sensor set."""

        return site_battery_entity_unique_ids(
            self._site_id, serial, BATTERY_ENTITY_UNIQUE_SUFFIXES
        )

    def battery_retired_sensor_unique_ids(self, serial: str) -> tuple[str, ...]:
        """Return retired unique IDs for a per-storage-battery sensor set."""

        return site_battery_entity_unique_ids(
            self._site_id, serial, BATTERY_RETIRED_UNIQUE_SUFFIXES
        )

    def ac_battery_sensor_unique_id(self, serial: str, suffix: str) -> str:
        """Return the unique ID for a per-AC-battery sensor."""

        return site_ac_battery_entity_unique_id(self._site_id, serial, suffix)

    def ac_battery_sensor_unique_ids(self, serial: str) -> tuple[str, ...]:
        """Return active unique IDs for a per-AC-battery sensor set."""

        return site_ac_battery_entity_unique_ids(
            self._site_id, serial, AC_BATTERY_ENTITY_UNIQUE_SUFFIXES
        )

    def ac_battery_retired_sensor_unique_ids(self, serial: str) -> tuple[str, ...]:
        """Return retired unique IDs for a per-AC-battery sensor set."""

        return site_ac_battery_entity_unique_ids(
            self._site_id, serial, AC_BATTERY_RETIRED_UNIQUE_SUFFIXES
        )

    @staticmethod
    def inverter_lifetime_sensor_unique_id(serial: str) -> str:
        """Return the unique ID for a microinverter lifetime energy sensor."""

        return inverter_entity_unique_id(serial)

    @staticmethod
    def inverter_telemetry_sensor_unique_id(serial: str) -> str:
        """Return the unique ID for a microinverter telemetry sensor."""

        return inverter_entity_unique_id(serial, "_telemetry")

    def sync_inverter_sensor_enabled_defaults(
        self,
        *,
        lifetime_energy_enabled: bool | None,
        power_enabled: bool | None,
    ) -> None:
        """Apply integration options to registered microinverter sensors."""

        for reg_entry in list(self._entity_registry_values()):
            if not self._registry_entry_matches_sensor(reg_entry):
                continue
            unique_id = _clean_text(getattr(reg_entry, "unique_id", None))
            if not unique_id or not unique_id.startswith(f"{DOMAIN}_inverter_"):
                continue
            if unique_id.endswith("_lifetime_energy"):
                enabled = lifetime_energy_enabled
            elif unique_id.endswith("_telemetry"):
                enabled = power_enabled
            else:
                continue
            if enabled is None:
                continue
            disabled_by = getattr(reg_entry, "disabled_by", None)
            if enabled:
                if not self._is_disabled_by_integration(disabled_by):
                    continue
                new_disabled_by = None
            else:
                if disabled_by is not None:
                    continue
                new_disabled_by = er.RegistryEntryDisabler.INTEGRATION
            self._ent_reg.async_update_entity(
                reg_entry.entity_id,
                disabled_by=new_disabled_by,
            )

    def remove_site_sensor_entity(self, key: str) -> None:
        """Remove a site-level sensor entity by setup key."""

        get_entity_id = getattr(self._ent_reg, "async_get_entity_id", None)
        if not callable(get_entity_id):
            return
        entity_id = get_entity_id("sensor", DOMAIN, self.site_sensor_unique_id(key))
        if entity_id is None:
            return
        self._ent_reg.async_remove(entity_id)
        self.known_site_entity_keys.discard(key)
        router_prefix = "gateway_iq_energy_router_"
        if key.startswith(router_prefix):
            self.known_gateway_iq_router_keys.discard(key[len(router_prefix) :])

    def remove_type_sensor_entity(self, type_key: str) -> None:
        """Remove a type inventory sensor entity by type key."""

        get_entity_id = getattr(self._ent_reg, "async_get_entity_id", None)
        if not callable(get_entity_id):
            return
        entity_id = get_entity_id(
            "sensor",
            DOMAIN,
            self.type_sensor_unique_id(type_key),
        )
        if entity_id is None:
            return
        self._ent_reg.async_remove(entity_id)
        self.known_type_keys.discard(type_key)

    def site_sensor_entity_registered(self, key: str) -> bool:
        """Return whether a site-level sensor entity is already registered."""

        get_entity_id = getattr(self._ent_reg, "async_get_entity_id", None)
        if not callable(get_entity_id):
            return False
        return (
            get_entity_id("sensor", DOMAIN, self.site_sensor_unique_id(key)) is not None
        )

    def remove_site_sensor_entities_with_prefix(self, prefix: str) -> None:
        """Remove site-level sensor entities with a unique ID prefix."""

        unique_prefix = self.site_sensor_unique_id(prefix)
        for reg_entry in list(self._entity_registry_values()):
            if not self._registry_entry_matches_sensor(reg_entry):
                continue
            unique_id = _clean_text(getattr(reg_entry, "unique_id", None))
            if not unique_id or not unique_id.startswith(unique_prefix):
                continue
            key = unique_id[len(f"{DOMAIN}_site_{self._site_id}_") :]
            self._ent_reg.async_remove(reg_entry.entity_id)
            self.known_site_entity_keys.discard(key)

    def prune_removed_gateway_iq_router_entities(
        self,
        current_router_keys: set[str],
    ) -> None:
        """Remove IQ Energy Router sensors no longer present in inventory."""

        entities = getattr(self._ent_reg, "entities", None)
        if not isinstance(entities, dict):
            return
        for reg_entry in list(entities.values()):
            if not self._registry_entry_matches_sensor(reg_entry):
                continue
            router_key = self._gateway_iq_router_key_from_unique_id(
                getattr(reg_entry, "unique_id", None)
            )
            if not router_key or router_key in current_router_keys:
                continue
            self._ent_reg.async_remove(reg_entry.entity_id)
            self.known_gateway_iq_router_keys.discard(router_key)
            self.known_site_entity_keys.discard(
                self.gateway_iq_router_entity_key(router_key)
            )

    def prune_dry_contact_type_inventory_entities(self) -> None:
        """Remove retired dry contact type inventory sensors."""

        entities = getattr(self._ent_reg, "entities", None)
        if not isinstance(entities, dict):
            return
        unique_prefix = f"{DOMAIN}_site_{self._site_id}_type_"
        unique_suffix = "_inventory"
        for reg_entry in list(entities.values()):
            if not self._registry_entry_matches_sensor(reg_entry):
                continue
            unique_id = _clean_text(getattr(reg_entry, "unique_id", None))
            type_key = None
            if unique_id and unique_id.startswith(unique_prefix):
                if not unique_id.endswith(unique_suffix):
                    continue
                type_key = unique_id[len(unique_prefix) : -len(unique_suffix)]
            else:
                entity_slug = reg_entry.entity_id.partition(".")[2]
                if "drycontactloads" not in entity_slug or not entity_slug.endswith(
                    "_inventory"
                ):
                    continue
                type_key = "drycontactloads"
            if not is_dry_contact_type_key(type_key):
                continue
            self._ent_reg.async_remove(reg_entry.entity_id)
            self.known_type_keys.discard(type_key)

    def prune_blocked_type_inventory_entities(
        self,
        blocked_type_keys: set[str],
    ) -> None:
        """Remove type inventory sensors hidden by dedicated sensor support."""

        entities = getattr(self._ent_reg, "entities", None)
        if not isinstance(entities, dict) or not blocked_type_keys:
            return
        unique_prefix = f"{DOMAIN}_site_{self._site_id}_type_"
        unique_suffix = "_inventory"
        for reg_entry in list(entities.values()):
            if not self._registry_entry_matches_sensor(reg_entry):
                continue
            unique_id = _clean_text(getattr(reg_entry, "unique_id", None))
            if not unique_id or not unique_id.startswith(unique_prefix):
                continue
            if not unique_id.endswith(unique_suffix):
                continue
            type_key = unique_id[len(unique_prefix) : -len(unique_suffix)]
            if type_key not in blocked_type_keys:
                continue
            self._ent_reg.async_remove(reg_entry.entity_id)
            self.known_type_keys.discard(type_key)

    def prune_historical_charger_sensor_entities(self) -> None:
        """Remove retired per-charger sensor entities."""

        entities = getattr(self._ent_reg, "entities", None)
        if not isinstance(entities, dict):
            return
        unique_prefix = f"{DOMAIN}_"
        for reg_entry in list(entities.values()):
            if not self._registry_entry_matches_sensor(reg_entry):
                continue
            unique_id = getattr(reg_entry, "unique_id", None)
            if not isinstance(unique_id, str) or not unique_id.startswith(
                unique_prefix
            ):
                continue
            if not unique_id.endswith(HISTORICAL_CHARGER_SENSOR_UNIQUE_SUFFIXES):
                continue
            self._ent_reg.async_remove(reg_entry.entity_id)

    def charger_sensor_unique_id(self, serial: str, suffix: str) -> str:
        """Return the unique ID for a per-charger sensor."""

        return charger_entity_unique_id(serial, suffix)

    def charger_sensor_unique_ids(self, serial: str) -> tuple[str, ...]:
        """Return active unique IDs for a per-charger sensor set."""

        return charger_entity_unique_ids(serial, CHARGER_SENSOR_UNIQUE_SUFFIXES)

    def charger_serial_from_unique_id(self, unique_id: object) -> str | None:
        """Return a charger serial parsed from a known per-charger unique ID."""

        return charger_entity_serial_from_unique_id(
            unique_id, CHARGER_SENSOR_UNIQUE_SUFFIXES
        )

    def prune_removed_charger_sensor_entities(self, current_set: set[str]) -> None:
        """Remove per-charger sensors no longer present in active discovery."""

        for reg_entry in list(self._entity_registry_values()):
            if not self._registry_entry_matches_sensor(reg_entry):
                continue
            unique_id = getattr(reg_entry, "unique_id", None)
            serial = self.charger_serial_from_unique_id(unique_id)
            if serial is None or serial in current_set:
                continue
            self._ent_reg.async_remove(reg_entry.entity_id)
            self.known_charger_serials.discard(serial)

    def remove_missing_charger_entities(self, current_set: set[str]) -> None:
        """Remove known charger entities no longer in the current set."""

        self._remove_missing_serial_entities(
            self.known_charger_serials,
            current_set,
            self.charger_sensor_unique_ids,
        )

    def prune_removed_site_entities(self) -> None:
        """Remove legacy site-level sensor entities that no longer exist."""

        get_entity_id = getattr(self._ent_reg, "async_get_entity_id", None)
        if not callable(get_entity_id):
            return
        for unique_id in (
            f"{DOMAIN}_site_{self._site_id}_gateway_connected_devices",
            f"{DOMAIN}_site_{self._site_id}_type_microinverter_inventory",
        ):
            entity_id = get_entity_id(
                "sensor",
                DOMAIN,
                unique_id,
            )
            if entity_id is not None:
                self._ent_reg.async_remove(entity_id)
        self.remove_site_sensor_entity("battery_inactive_microinverters")

    def prune_battery_registry_once(self, current_set: set[str]) -> None:
        """Prune stale storage battery registry entries after startup."""

        if self.battery_registry_pruned:
            return
        for reg_entry in list(self._entity_registry_values()):
            if not self._registry_entry_matches_sensor(reg_entry):
                continue
            unique_id = getattr(reg_entry, "unique_id", None) or ""
            serial = self.battery_serial_from_unique_id(unique_id)
            if serial is None:
                continue
            if any(
                unique_id.endswith(suffix) for suffix in BATTERY_RETIRED_UNIQUE_SUFFIXES
            ):
                self._ent_reg.async_remove(reg_entry.entity_id)
                self.known_battery_serials.discard(serial)
                continue
            if serial in current_set:
                continue
            self._ent_reg.async_remove(reg_entry.entity_id)
            self.known_battery_serials.discard(serial)
        self.battery_registry_pruned = True

    def remove_missing_battery_entities(self, current_set: set[str]) -> None:
        """Remove known storage battery entities no longer in the current set."""

        self._remove_missing_serial_entities(
            self.known_battery_serials,
            current_set,
            lambda serial: (
                *self.battery_sensor_unique_ids(serial),
                *self.battery_retired_sensor_unique_ids(serial),
            ),
        )

    def prune_ac_battery_registry_once(self, current_set: set[str]) -> None:
        """Prune stale AC battery registry entries after startup."""

        if self.ac_battery_registry_pruned:
            return
        for reg_entry in list(self._entity_registry_values()):
            if not self._registry_entry_matches_sensor(reg_entry):
                continue
            unique_id = getattr(reg_entry, "unique_id", None) or ""
            serial = self.ac_battery_serial_from_unique_id(unique_id)
            if serial is None:
                continue
            if any(
                unique_id.endswith(suffix)
                for suffix in AC_BATTERY_RETIRED_UNIQUE_SUFFIXES
            ):
                self._ent_reg.async_remove(reg_entry.entity_id)
                self.known_ac_battery_serials.discard(serial)
                continue
            if serial in current_set:
                continue
            self._ent_reg.async_remove(reg_entry.entity_id)
            self.known_ac_battery_serials.discard(serial)
        self.ac_battery_registry_pruned = True

    def remove_missing_ac_battery_entities(self, current_set: set[str]) -> None:
        """Remove known AC battery entities no longer in the current set."""

        self._remove_missing_serial_entities(
            self.known_ac_battery_serials,
            current_set,
            lambda serial: (
                *self.ac_battery_sensor_unique_ids(serial),
                *self.ac_battery_retired_sensor_unique_ids(serial),
            ),
        )

    def prune_inverter_registry_once(self, current_set: set[str]) -> None:
        """Prune stale inverter registry entries after startup."""

        if self.inverter_registry_pruned:
            return
        unique_prefix = f"{DOMAIN}_inverter_"
        for reg_entry in list(self._entity_registry_values()):
            if not self._registry_entry_matches_sensor(reg_entry):
                continue
            unique_id = getattr(reg_entry, "unique_id", None) or ""
            if not (
                isinstance(unique_id, str)
                and unique_id.startswith(unique_prefix)
                and unique_id.endswith(INVERTER_ENTITY_UNIQUE_SUFFIXES)
            ):
                continue
            serial = inverter_entity_serial_from_unique_id(unique_id)
            if not serial or serial in current_set:
                continue
            self._ent_reg.async_remove(reg_entry.entity_id)
            self.known_inverter_serials.discard(serial)
            self.known_inverter_telemetry_serials.discard(serial)
        self.inverter_registry_pruned = True

    def remove_missing_inverter_entities(self, current_set: set[str]) -> None:
        """Remove known inverter entities no longer in the current set."""

        self._remove_missing_serial_entities(
            self.known_inverter_serials,
            current_set,
            lambda serial: (
                self.inverter_lifetime_sensor_unique_id(serial),
                self.inverter_telemetry_sensor_unique_id(serial),
            ),
        )
        self.known_inverter_telemetry_serials.intersection_update(current_set)

    def battery_serial_from_unique_id(self, unique_id: object) -> str | None:
        """Return a battery serial parsed from a known per-battery unique ID."""

        return battery_entity_serial_from_unique_id(
            unique_id,
            site_id=self._site_id,
            suffixes=(
                *BATTERY_ENTITY_UNIQUE_SUFFIXES,
                *BATTERY_RETIRED_UNIQUE_SUFFIXES,
            ),
        )

    def ac_battery_serial_from_unique_id(self, unique_id: object) -> str | None:
        """Return an AC battery serial parsed from a known unique ID."""

        return ac_battery_entity_serial_from_unique_id(
            unique_id,
            site_id=self._site_id,
            suffixes=(
                *AC_BATTERY_ENTITY_UNIQUE_SUFFIXES,
                *AC_BATTERY_RETIRED_UNIQUE_SUFFIXES,
            ),
        )

    def _gateway_iq_router_key_from_unique_id(self, unique_id: object) -> str | None:
        key = _clean_text(unique_id)
        if not key:
            return None
        prefix = f"{DOMAIN}_site_{self._site_id}_gateway_iq_energy_router_"
        if not key.startswith(prefix):
            return None
        router_key = key[len(prefix) :]
        return router_key or None

    def _entity_registry_values(self) -> Iterable[Any]:
        entities = getattr(self._ent_reg, "entities", None)
        values = getattr(entities, "values", None)
        if callable(values):
            return cast(Iterable[Any], values())
        return ()

    def _registry_entry_matches_sensor(self, reg_entry: Any) -> bool:
        entry_domain = getattr(reg_entry, "domain", None)
        if entry_domain is None:
            entry_domain = reg_entry.entity_id.partition(".")[0]
        if entry_domain != "sensor":
            return False
        entry_platform = getattr(reg_entry, "platform", None)
        if entry_platform is not None and entry_platform != DOMAIN:
            return False
        entry_config_id = getattr(reg_entry, "config_entry_id", None)
        return not (
            entry_config_id is not None and entry_config_id != self._config_entry_id
        )

    @staticmethod
    def _is_disabled_by_integration(disabled_by: object) -> bool:
        if disabled_by is None:
            return False
        return getattr(disabled_by, "value", disabled_by) == "integration"

    def _async_get_sensor_entity_id(self, unique_id: str) -> str | None:
        get_entity_id = getattr(self._ent_reg, "async_get_entity_id", None)
        if not callable(get_entity_id):
            return None
        return cast(str | None, get_entity_id("sensor", DOMAIN, unique_id))

    def _remove_missing_serial_entities(
        self,
        known_serials: set[str],
        current_set: set[str],
        unique_ids_for_serial: Callable[[str], Iterable[str]],
    ) -> None:
        """Remove entities for serials that disappeared from active discovery."""

        removed_serials = known_serials - current_set
        for serial in removed_serials:
            for unique_id in unique_ids_for_serial(serial):
                entity_id = self._async_get_sensor_entity_id(unique_id)
                if entity_id is not None:
                    self._ent_reg.async_remove(entity_id)
            known_serials.discard(serial)

        known_serials.intersection_update(current_set)
