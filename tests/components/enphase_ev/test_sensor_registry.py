from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.sensor_registry import EnphaseSensorRegistrySetup

ENTRY_ID = "entry-id"
SITE_ID = "site-id"


class FakeRegistry:
    def __init__(self, entities: dict[str, SimpleNamespace] | None = None) -> None:
        self.entities = entities or {}
        self.removed: list[str] = []

    def async_get_entity_id(
        self,
        domain: str,
        platform: str,
        unique_id: str,
    ) -> str | None:
        for entity_id, entry in self.entities.items():
            if (
                domain == "sensor"
                and platform == DOMAIN
                and getattr(entry, "unique_id", None) == unique_id
            ):
                return entity_id
        return None

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)


def _entry(
    entity_id: str,
    unique_id: str,
    *,
    domain: str = "sensor",
    platform: str = DOMAIN,
    config_entry_id: str = ENTRY_ID,
) -> SimpleNamespace:
    return SimpleNamespace(
        entity_id=entity_id,
        unique_id=unique_id,
        domain=domain,
        platform=platform,
        config_entry_id=config_entry_id,
    )


def _helper(registry: object) -> EnphaseSensorRegistrySetup:
    return EnphaseSensorRegistrySetup(
        registry,
        config_entry_id=ENTRY_ID,
        site_id=SITE_ID,
    )


def test_sensor_registry_serial_parsers_ignore_non_string_unique_ids() -> None:
    helper = _helper(SimpleNamespace())

    assert (
        helper.battery_sensor_unique_id("BAT-1", "_status")
        == f"{DOMAIN}_site_{SITE_ID}_battery_BAT-1_status"
    )
    assert (
        helper.ac_battery_sensor_unique_id("ACBAT-1", "_status")
        == f"{DOMAIN}_site_{SITE_ID}_ac_battery_ACBAT-1_status"
    )
    assert helper.battery_serial_from_unique_id(None) is None
    assert helper.ac_battery_serial_from_unique_id(None) is None


def test_sensor_registry_get_entity_id_without_registry_method() -> None:
    helper = _helper(SimpleNamespace())

    assert helper._async_get_sensor_entity_id("unique-id") is None


def test_sensor_registry_type_removal_and_entity_iteration_edge_paths() -> None:
    helper = _helper(SimpleNamespace(entities=None))
    helper.remove_type_sensor_entity("envoy")
    assert helper._entity_registry_values() == ()  # noqa: SLF001

    registry = SimpleNamespace(
        async_get_entity_id=MagicMock(return_value=None),
        async_remove=MagicMock(),
    )
    helper = _helper(registry)
    helper.remove_type_sensor_entity("envoy")
    registry.async_remove.assert_not_called()


def test_sensor_registry_prunes_gateway_and_type_inventory_entities() -> None:
    registry = FakeRegistry(
        {
            "sensor.old_router": _entry(
                "sensor.old_router",
                f"{DOMAIN}_site_{SITE_ID}_gateway_iq_energy_router_old",
            ),
            "sensor.keep_router": _entry(
                "sensor.keep_router",
                f"{DOMAIN}_site_{SITE_ID}_gateway_iq_energy_router_keep",
            ),
            "sensor.wrong_entry_router": _entry(
                "sensor.wrong_entry_router",
                f"{DOMAIN}_site_{SITE_ID}_gateway_iq_energy_router_wrong_entry",
                config_entry_id="other-entry",
            ),
            "sensor.dry_contact": _entry(
                "sensor.dry_contact",
                f"{DOMAIN}_site_{SITE_ID}_type_drycontactloads_inventory",
            ),
            "sensor.blocked_encharge": _entry(
                "sensor.blocked_encharge",
                f"{DOMAIN}_site_{SITE_ID}_type_encharge_inventory",
            ),
            "sensor.keep_type": _entry(
                "sensor.keep_type",
                f"{DOMAIN}_site_{SITE_ID}_type_envoy_inventory",
            ),
        }
    )
    helper = _helper(registry)
    helper.known_gateway_iq_router_keys.update({"old", "keep"})
    helper.known_site_entity_keys.update(
        {
            "gateway_iq_energy_router_old",
            "gateway_iq_energy_router_keep",
        }
    )
    helper.known_type_keys.update({"drycontactloads", "encharge", "envoy"})

    helper.prune_removed_gateway_iq_router_entities({"keep"})
    helper.prune_dry_contact_type_inventory_entities()
    helper.prune_blocked_type_inventory_entities({"encharge"})

    assert Counter(registry.removed) == Counter(
        {
            "sensor.old_router",
            "sensor.dry_contact",
            "sensor.blocked_encharge",
        }
    )
    assert helper.known_gateway_iq_router_keys == {"keep"}
    assert helper.known_site_entity_keys == {"gateway_iq_energy_router_keep"}
    assert helper.known_type_keys == {"envoy"}


def test_sensor_registry_prunes_historical_and_removed_site_entities() -> None:
    registry = FakeRegistry(
        {
            "sensor.connector_reason": _entry(
                "sensor.connector_reason",
                f"{DOMAIN}_EV1_connector_reason",
            ),
            "sensor.keep_power": _entry(
                "sensor.keep_power",
                f"{DOMAIN}_EV1_power",
            ),
            "sensor.ignore_platform": _entry(
                "sensor.ignore_platform",
                f"{DOMAIN}_EV1_connector_reason",
                platform="other",
            ),
            "sensor.gateway_connected_devices": _entry(
                "sensor.gateway_connected_devices",
                f"{DOMAIN}_site_{SITE_ID}_gateway_connected_devices",
            ),
            "sensor.microinverter_inventory": _entry(
                "sensor.microinverter_inventory",
                f"{DOMAIN}_site_{SITE_ID}_type_microinverter_inventory",
            ),
            "sensor.battery_inactive_microinverters": _entry(
                "sensor.battery_inactive_microinverters",
                f"{DOMAIN}_site_{SITE_ID}_battery_inactive_microinverters",
            ),
        }
    )
    helper = _helper(registry)
    helper.known_site_entity_keys.add("battery_inactive_microinverters")

    helper.prune_historical_charger_sensor_entities()
    helper.prune_removed_site_entities()

    assert Counter(registry.removed) == Counter(
        {
            "sensor.connector_reason",
            "sensor.gateway_connected_devices",
            "sensor.microinverter_inventory",
            "sensor.battery_inactive_microinverters",
        }
    )
    assert "battery_inactive_microinverters" not in helper.known_site_entity_keys


def test_sensor_registry_prunes_removed_charger_sensor_entities() -> None:
    registry = FakeRegistry(
        {
            "sensor.old_power": _entry(
                "sensor.old_power",
                f"{DOMAIN}_EVOLD_power",
            ),
            "sensor.old_status": _entry(
                "sensor.old_status",
                f"{DOMAIN}_EVOLD_status",
            ),
            "sensor.keep_power": _entry(
                "sensor.keep_power",
                f"{DOMAIN}_EVKEEP_power",
            ),
            "sensor.keep_connector_status": _entry(
                "sensor.keep_connector_status",
                f"{DOMAIN}_EVKEEP_connector_status",
            ),
            "sensor.keep_charger_authentication": _entry(
                "sensor.keep_charger_authentication",
                f"{DOMAIN}_EVKEEP_charger_authentication",
            ),
            "sensor.missing_power": _entry(
                "sensor.missing_power",
                f"{DOMAIN}_EVMISSING_power",
            ),
            "sensor.keep_site_status": _entry(
                "sensor.keep_site_status",
                f"{DOMAIN}_site_{SITE_ID}_service_status",
            ),
            "sensor.ignore_wrong_entry": _entry(
                "sensor.ignore_wrong_entry",
                f"{DOMAIN}_EVOTHER_power",
                config_entry_id="other-entry",
            ),
        }
    )
    helper = _helper(registry)
    helper.known_charger_serials.update({"EVKEEP", "EVMISSING"})

    helper.prune_removed_charger_sensor_entities({"EVKEEP"})
    helper.remove_missing_charger_entities({"EVKEEP"})
    registry.entities["sensor.lookup_status"] = _entry(
        "sensor.lookup_status",
        helper.charger_sensor_unique_id("EVLOOKUP", "_status"),
    )
    helper.known_charger_serials.add("EVLOOKUP")
    helper.remove_missing_charger_entities({"EVKEEP"})

    assert Counter(registry.removed) == Counter(
        {
            "sensor.old_power",
            "sensor.old_status",
            "sensor.missing_power",
            "sensor.lookup_status",
        }
    )
    assert helper.known_charger_serials == {"EVKEEP"}
    assert (
        helper.charger_serial_from_unique_id(f"{DOMAIN}_EVKEEP_connector_status")
        == "EVKEEP"
    )
    assert (
        helper.charger_serial_from_unique_id(f"{DOMAIN}_EVKEEP_charger_authentication")
        == "EVKEEP"
    )
    assert helper.charger_serial_from_unique_id("other_EV_power") is None
    assert (
        helper.charger_serial_from_unique_id(f"{DOMAIN}_site_{SITE_ID}_service_status")
        is None
    )
    assert (
        helper.charger_serial_from_unique_id(f"{DOMAIN}_inverter_INV_lifetime_energy")
        is None
    )
    assert helper.charger_serial_from_unique_id(f"{DOMAIN}_EV_unknown") is None


def test_sensor_registry_prunes_battery_ac_battery_and_inverter_entities() -> None:
    registry = FakeRegistry(
        {
            "sensor.battery_old_status": _entry(
                "sensor.battery_old_status",
                f"{DOMAIN}_site_{SITE_ID}_battery_OLD_status",
            ),
            "sensor.battery_retired_last_reported": _entry(
                "sensor.battery_retired_last_reported",
                f"{DOMAIN}_site_{SITE_ID}_battery_RETIRED_last_reported",
            ),
            "sensor.battery_keep_status": _entry(
                "sensor.battery_keep_status",
                f"{DOMAIN}_site_{SITE_ID}_battery_KEEP_status",
            ),
            "sensor.battery_missing_status": _entry(
                "sensor.battery_missing_status",
                f"{DOMAIN}_site_{SITE_ID}_battery_MISSING_status",
            ),
            "sensor.ac_battery_old_status": _entry(
                "sensor.ac_battery_old_status",
                f"{DOMAIN}_site_{SITE_ID}_ac_battery_ACOLD_status",
            ),
            "sensor.ac_battery_missing_power": _entry(
                "sensor.ac_battery_missing_power",
                f"{DOMAIN}_site_{SITE_ID}_ac_battery_ACMISSING_power",
            ),
            "sensor.inverter_old_lifetime": _entry(
                "sensor.inverter_old_lifetime",
                f"{DOMAIN}_inverter_INVOLD_lifetime_energy",
            ),
            "sensor.inverter_keep_lifetime": _entry(
                "sensor.inverter_keep_lifetime",
                f"{DOMAIN}_inverter_INVKEEP_lifetime_energy",
            ),
            "sensor.inverter_missing_lifetime": _entry(
                "sensor.inverter_missing_lifetime",
                f"{DOMAIN}_inverter_INVMISSING_lifetime_energy",
            ),
            "sensor.ignore_wrong_entry": _entry(
                "sensor.ignore_wrong_entry",
                f"{DOMAIN}_site_{SITE_ID}_battery_WRONG_status",
                config_entry_id="other-entry",
            ),
        }
    )
    helper = _helper(registry)
    helper.known_battery_serials.update({"KEEP", "MISSING"})
    helper.known_ac_battery_serials.update({"ACKEEP", "ACMISSING"})
    helper.known_inverter_serials.update({"INVKEEP", "INVMISSING"})

    helper.prune_battery_registry_once({"KEEP"})
    helper.remove_missing_battery_entities({"KEEP"})
    helper.prune_ac_battery_registry_once({"ACKEEP"})
    helper.remove_missing_ac_battery_entities({"ACKEEP"})
    assert helper.inverter_lifetime_sensor_unique_id("INV") == (
        f"{DOMAIN}_inverter_INV_lifetime_energy"
    )
    helper.prune_inverter_registry_once({"INVKEEP", "INVMISSING"})
    helper.remove_missing_inverter_entities({"INVKEEP"})
    helper.prune_inverter_registry_once({"INVKEEP"})

    assert Counter(registry.removed) == Counter(
        {
            "sensor.battery_old_status",
            "sensor.battery_retired_last_reported",
            "sensor.battery_missing_status",
            "sensor.ac_battery_old_status",
            "sensor.ac_battery_missing_power",
            "sensor.inverter_old_lifetime",
            "sensor.inverter_missing_lifetime",
        }
    )
    assert helper.known_battery_serials == {"KEEP"}
    assert helper.known_ac_battery_serials == {"ACKEEP"}
    assert helper.known_inverter_serials == {"INVKEEP"}
    assert helper.battery_registry_pruned is True
    assert helper.ac_battery_registry_pruned is True
    assert helper.inverter_registry_pruned is True


def test_sensor_registry_remove_helpers_use_registry_lookup() -> None:
    registry = SimpleNamespace(
        async_get_entity_id=MagicMock(return_value="sensor.remove_me"),
        async_remove=MagicMock(),
    )
    helper = _helper(registry)
    helper.known_site_entity_keys.add("gateway_iq_energy_router_old")
    helper.known_gateway_iq_router_keys.add("old")
    helper.known_type_keys.add("envoy")

    helper.remove_site_sensor_entity("gateway_iq_energy_router_old")
    helper.remove_type_sensor_entity("envoy")

    registry.async_remove.assert_any_call("sensor.remove_me")
    assert registry.async_remove.call_count == 2
    assert helper.known_site_entity_keys == set()
    assert helper.known_gateway_iq_router_keys == set()
    assert helper.known_type_keys == set()
