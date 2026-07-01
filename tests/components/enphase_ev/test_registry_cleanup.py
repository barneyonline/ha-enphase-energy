from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.enphase_ev import (
    DOMAIN,
    _prune_inactive_serial_entities,
    _remove_empty_inactive_serial_devices,
    _serial_entity_group_from_unique_id,
)
from custom_components.enphase_ev.const import CONF_SITE_ID
from custom_components.enphase_ev.serial_discovery import (
    active_ac_battery_serials_for_cleanup,
    active_battery_serials_for_cleanup,
    active_charger_serials_for_cleanup,
    active_inverter_serials_for_cleanup,
    active_serial_registry_identifiers,
    all_active_serial_registry_identifiers,
    inventory_type_available_for_cleanup,
    inventory_type_bucket_empty_for_cleanup,
    inventory_type_bucket_for_cleanup,
    inventory_type_selected_for_cleanup,
    serials_from_getter,
)
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL
from custom_components.enphase_ev.serial_entity_metadata import (
    charger_entity_serial_from_unique_id,
    prefixed_serial_from_unique_id,
)


@pytest.mark.asyncio
async def test_remove_empty_inactive_serial_devices_keeps_devices_with_entities(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    active_serial = RANDOM_SERIAL
    inactive_serial = "EV-RETIRED"

    active_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, active_serial)},
        manufacturer="Enphase",
        name="Active Charger",
    )
    inactive_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, inactive_serial)},
        manufacturer="Enphase",
        name="Retired Charger",
    )
    type_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="Gateway",
    )
    stale_entity = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{inactive_serial}_status",
        config_entry=config_entry,
        device_id=inactive_device.id,
    )
    coord = SimpleNamespace(
        _devices_inventory_ready=True,
        _selected_type_keys={"iqevse"},
        inventory_view=SimpleNamespace(has_type_for_entities=lambda _key: False),
        iter_serials=lambda: [active_serial],
        iter_battery_serials=lambda: [],
        iter_ac_battery_serials=lambda: [],
        iter_inverter_serials=lambda: [],
    )

    assert (
        _remove_empty_inactive_serial_devices(
            hass, config_entry, coord, dev_reg, site_id
        )
        == 0
    )
    assert dev_reg.async_get(inactive_device.id) is not None

    ent_reg.async_remove(stale_entity.entity_id)

    assert (
        _remove_empty_inactive_serial_devices(
            hass, config_entry, coord, dev_reg, site_id
        )
        == 1
    )
    assert dev_reg.async_get(active_device.id) is not None
    assert dev_reg.async_get(inactive_device.id) is None
    assert dev_reg.async_get(type_device.id) is not None


@pytest.mark.asyncio
async def test_remove_empty_inactive_serial_devices_preserves_active_supported_devices(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    active_battery = "BAT-ACTIVE"
    active_inverter = "INV-ACTIVE"
    inactive_battery = "BAT-RETIRED"

    active_battery_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, active_battery)},
        manufacturer="Enphase",
        name="Active Battery",
    )
    active_inverter_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, active_inverter)},
        manufacturer="Enphase",
        name="Active Microinverter",
    )
    inactive_battery_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, inactive_battery)},
        manufacturer="Enphase",
        name="Retired Battery",
    )
    stale_entity = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_battery_{inactive_battery}_status",
        config_entry=config_entry,
        device_id=inactive_battery_device.id,
    )
    coord = SimpleNamespace(
        _devices_inventory_ready=True,
        _selected_type_keys={"encharge", "microinverter"},
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda key: key in {"encharge", "microinverter"}
        ),
        _battery_status_payload={},
        include_inverters=True,
        _inverters_inventory_payload={},
        iter_serials=lambda: [],
        iter_battery_serials=lambda: [active_battery],
        iter_ac_battery_serials=lambda: [],
        iter_inverter_serials=lambda: [active_inverter],
    )

    assert all_active_serial_registry_identifiers(coord) == {
        active_battery,
        active_inverter,
    }
    assert (
        _remove_empty_inactive_serial_devices(
            hass, config_entry, coord, dev_reg, site_id
        )
        == 0
    )

    ent_reg.async_remove(stale_entity.entity_id)

    assert (
        _remove_empty_inactive_serial_devices(
            hass, config_entry, coord, dev_reg, site_id
        )
        == 1
    )
    assert dev_reg.async_get(active_battery_device.id) is not None
    assert dev_reg.async_get(active_inverter_device.id) is not None
    assert dev_reg.async_get(inactive_battery_device.id) is None


@pytest.mark.asyncio
async def test_prune_inactive_serial_entities_removes_retired_charger_entities(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    ent_reg = er.async_get(hass)
    active_serial = RANDOM_SERIAL
    inactive_serial = "EV-RETIRED"
    active_battery = "BAT-ACTIVE"
    inactive_battery = "BAT-RETIRED"
    active_ac_battery = "ACBAT-ACTIVE"
    inactive_ac_battery = "ACBAT-RETIRED"
    active_inverter = "INV-ACTIVE"
    inactive_inverter = "INV-RETIRED"
    coord = SimpleNamespace(
        _devices_inventory_ready=True,
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda key: key
            in {"encharge", "ac_battery", "microinverter"}
        ),
        _battery_status_payload={},
        battery_has_acb=True,
        _ac_battery_devices_payload={},
        include_inverters=True,
        _inverters_inventory_payload={},
        iter_serials=lambda: [active_serial],
        iter_battery_serials=lambda: [active_battery],
        iter_ac_battery_serials=lambda: [active_ac_battery],
        iter_inverter_serials=lambda: [active_inverter],
    )

    stale_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{inactive_serial}_charging_amps",
        config_entry=config_entry,
    )
    stale_binary = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{DOMAIN}_{inactive_serial}_connected",
        config_entry=config_entry,
    )
    active_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{active_serial}_last_rpt",
        config_entry=config_entry,
    )
    active_connector_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{active_serial}_connector_status",
        config_entry=config_entry,
    )
    active_authentication_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{active_serial}_charger_authentication",
        config_entry=config_entry,
    )
    site_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_service_status",
        config_entry=config_entry,
    )
    inverter_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_inverter_{active_inverter}_lifetime_energy",
        config_entry=config_entry,
    )
    stale_battery_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_battery_{inactive_battery}_status",
        config_entry=config_entry,
    )
    active_battery_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_battery_{active_battery}_status",
        config_entry=config_entry,
    )
    stale_ac_battery_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_ac_battery_{inactive_ac_battery}_power",
        config_entry=config_entry,
    )
    active_ac_battery_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_ac_battery_{active_ac_battery}_power",
        config_entry=config_entry,
    )
    stale_inverter_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_inverter_{inactive_inverter}_lifetime_energy",
        config_entry=config_entry,
    )

    assert _prune_inactive_serial_entities(hass, config_entry, coord, site_id) == 5

    assert ent_reg.async_get(stale_sensor.entity_id) is None
    assert ent_reg.async_get(stale_binary.entity_id) is None
    assert ent_reg.async_get(stale_battery_sensor.entity_id) is None
    assert ent_reg.async_get(stale_ac_battery_sensor.entity_id) is None
    assert ent_reg.async_get(stale_inverter_sensor.entity_id) is None
    assert ent_reg.async_get(active_sensor.entity_id) is not None
    assert ent_reg.async_get(active_connector_sensor.entity_id) is not None
    assert ent_reg.async_get(active_authentication_sensor.entity_id) is not None
    assert ent_reg.async_get(active_battery_sensor.entity_id) is not None
    assert ent_reg.async_get(active_ac_battery_sensor.entity_id) is not None
    assert ent_reg.async_get(site_sensor.entity_id) is not None
    assert ent_reg.async_get(inverter_sensor.entity_id) is not None


@pytest.mark.asyncio
async def test_prune_inactive_serial_entities_skips_unknown_device_families(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    ent_reg = er.async_get(hass)
    inactive_serial = "EV-RETIRED"
    inactive_battery = "BAT-UNKNOWN"
    inactive_ac_battery = "ACBAT-UNKNOWN"
    inactive_inverter = "INV-UNKNOWN"
    coord = SimpleNamespace(
        _devices_inventory_ready=True,
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda key: key
            in {"encharge", "ac_battery", "microinverter"}
        ),
        battery_has_acb=True,
        include_inverters=True,
        _battery_status_payload=None,
        _ac_battery_devices_payload=None,
        _inverters_inventory_payload=None,
        iter_serials=lambda: [RANDOM_SERIAL],
        iter_battery_serials=lambda: [],
        iter_ac_battery_serials=lambda: [],
        iter_inverter_serials=lambda: [],
    )

    stale_charger_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{inactive_serial}_charging_amps",
        config_entry=config_entry,
    )
    stale_battery_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_battery_{inactive_battery}_status",
        config_entry=config_entry,
    )
    stale_ac_battery_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_ac_battery_{inactive_ac_battery}_status",
        config_entry=config_entry,
    )
    stale_inverter_sensor = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_inverter_{inactive_inverter}_lifetime_energy",
        config_entry=config_entry,
    )

    assert active_serial_registry_identifiers(coord) == {
        "charger": {RANDOM_SERIAL},
        "battery": None,
        "ac_battery": None,
        "inverter": None,
    }
    assert _prune_inactive_serial_entities(hass, config_entry, coord, site_id) == 1

    assert ent_reg.async_get(stale_charger_sensor.entity_id) is None
    assert ent_reg.async_get(stale_battery_sensor.entity_id) is not None
    assert ent_reg.async_get(stale_ac_battery_sensor.entity_id) is not None
    assert ent_reg.async_get(stale_inverter_sensor.entity_id) is not None


@pytest.mark.asyncio
async def test_remove_empty_inactive_serial_devices_waits_for_authoritative_families(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    inactive_serial = "SERIAL-UNKNOWN-FAMILY"
    inactive_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, inactive_serial)},
        manufacturer="Enphase",
        name="Empty Serial Device",
    )
    coord = SimpleNamespace(
        _devices_inventory_ready=True,
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda key: key
            in {"encharge", "ac_battery", "microinverter"}
        ),
        battery_has_acb=True,
        include_inverters=True,
        _battery_status_payload=None,
        _ac_battery_devices_payload=None,
        _inverters_inventory_payload=None,
        iter_serials=lambda: [],
        iter_battery_serials=lambda: [],
        iter_ac_battery_serials=lambda: [],
        iter_inverter_serials=lambda: [],
    )

    assert (
        _remove_empty_inactive_serial_devices(
            hass, config_entry, coord, dev_reg, site_id
        )
        == 0
    )
    assert dev_reg.async_get(inactive_device.id) is not None


def test_prune_inactive_serial_entities_handles_guard_paths(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    module = importlib.import_module("custom_components.enphase_ev")
    coord_not_ready = SimpleNamespace(
        _devices_inventory_ready=False,
        iter_serials=lambda: [],
    )

    assert (
        _prune_inactive_serial_entities(hass, config_entry, coord_not_ready, site_id)
        == 0
    )
    assert charger_entity_serial_from_unique_id(None, ("_power",)) is None
    assert charger_entity_serial_from_unique_id("other_EV_power", ("_power",)) is None
    assert (
        charger_entity_serial_from_unique_id(f"{DOMAIN}_EV_unknown", ("_power",))
        is None
    )

    coord_ready = SimpleNamespace(
        _devices_inventory_ready=True,
        iter_serials=lambda: [],
    )
    original_er = module.er
    monkeypatch.setattr(module, "er", None)
    assert (
        _prune_inactive_serial_entities(hass, config_entry, coord_ready, site_id) == 0
    )
    monkeypatch.setattr(module, "er", original_er)

    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: (_ for _ in ()).throw(RuntimeError("registry boom")),
    )
    assert (
        _prune_inactive_serial_entities(hass, config_entry, coord_ready, site_id) == 0
    )

    removed: list[str] = []

    class FakeRegistry:
        entities = {
            "light.skip": SimpleNamespace(
                entity_id="light.skip",
                domain="light",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_EVOLD_power",
                config_entry_id=config_entry.entry_id,
            ),
            "sensor.wrong_entry": SimpleNamespace(
                entity_id="sensor.wrong_entry",
                domain="sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_EVOLD_power",
                config_entry_id="other-entry",
            ),
            "sensor.no_entity_id": SimpleNamespace(
                entity_id=None,
                domain="sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_EVOLD_power",
                config_entry_id=config_entry.entry_id,
            ),
            "sensor.raise": SimpleNamespace(
                entity_id="sensor.raise",
                domain="sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_EVOLD_status",
                config_entry_id=config_entry.entry_id,
            ),
            "sensor.domain_from_entity": SimpleNamespace(
                entity_id="sensor.domain_from_entity",
                domain=None,
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_EVOLD_charging_amps",
                config_entry_id=config_entry.entry_id,
            ),
        }

        def async_remove(self, entity_id):
            if entity_id == "sensor.raise":
                raise RuntimeError("remove boom")
            removed.append(entity_id)

    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: FakeRegistry(),
    )

    assert (
        _prune_inactive_serial_entities(hass, config_entry, coord_ready, site_id) == 1
    )
    assert removed == ["sensor.domain_from_entity"]


def test_serial_registry_identifier_helpers_cover_edge_paths(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]

    class BadIter:
        def __iter__(self):
            raise RuntimeError("boom")

    class BadSelected(tuple):
        def __iter__(self):
            raise RuntimeError("selected boom")

    class BadSite:
        def __str__(self) -> str:
            raise RuntimeError("site boom")

    class EmptyCoord:
        pass

    def _raise_type(_key):
        raise RuntimeError("type boom")

    assert inventory_type_available_for_cleanup(EmptyCoord(), "encharge") is None
    assert (
        inventory_type_available_for_cleanup(
            SimpleNamespace(
                inventory_view=SimpleNamespace(has_type_for_entities=_raise_type)
            ),
            "encharge",
        )
        is None
    )
    assert inventory_type_selected_for_cleanup(EmptyCoord(), " ") is False
    assert (
        inventory_type_selected_for_cleanup(
            SimpleNamespace(_selected_type_keys=BadSelected(("encharge",))),
            "encharge",
        )
        is True
    )
    assert inventory_type_bucket_for_cleanup(EmptyCoord(), " ") is None
    assert (
        inventory_type_bucket_empty_for_cleanup(
            SimpleNamespace(_type_device_buckets={"encharge": {"devices": []}}),
            "encharge",
        )
        is False
    )
    assert serials_from_getter(None) is None
    assert active_serial_registry_identifiers(
        SimpleNamespace(_devices_inventory_ready=False)
    ) == {
        "charger": None,
        "battery": None,
        "ac_battery": None,
        "inverter": None,
    }
    assert (
        active_serial_registry_identifiers(
            SimpleNamespace(
                _devices_inventory_ready=True,
                _active_inventory_evse_serials=lambda: (_ for _ in ()).throw(
                    RuntimeError("evse boom")
                ),
            )
        )["charger"]
        is None
    )
    assert (
        active_serial_registry_identifiers(
            SimpleNamespace(
                _devices_inventory_ready=True,
                _active_inventory_evse_serials=lambda: None,
            )
        )["charger"]
        is None
    )
    assert (
        active_serial_registry_identifiers(
            SimpleNamespace(
                _devices_inventory_ready=True,
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda key: key == "ac_battery"
                ),
                battery_has_acb=False,
                iter_serials=lambda: [],
            )
        )["ac_battery"]
        == set()
    )
    assert (
        active_serial_registry_identifiers(
            SimpleNamespace(
                _devices_inventory_ready=True,
                include_inverters=False,
                iter_serials=lambda: [],
            )
        )["inverter"]
        == set()
    )
    assert active_charger_serials_for_cleanup(
        SimpleNamespace(_devices_inventory_ready=True, iter_serials=lambda: ["EV-1"])
    ) == {"EV-1"}
    assert active_battery_serials_for_cleanup(
        SimpleNamespace(
            _devices_inventory_ready=True,
            inventory_view=SimpleNamespace(
                has_type_for_entities=lambda key: key == "encharge"
            ),
            _battery_status_payload={},
            iter_battery_serials=lambda: ["BAT-1"],
        )
    ) == {"BAT-1"}
    assert (
        active_battery_serials_for_cleanup(
            SimpleNamespace(
                _devices_inventory_ready=True,
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda _key: False
                ),
                iter_battery_serials=lambda: ["BAT-STALE"],
            )
        )
        is None
    )
    assert (
        active_battery_serials_for_cleanup(
            SimpleNamespace(
                _devices_inventory_ready=True,
                _selected_type_keys={"iqevse"},
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda _key: False
                ),
                iter_battery_serials=lambda: ["BAT-STALE"],
            )
        )
        == set()
    )
    assert (
        active_battery_serials_for_cleanup(
            SimpleNamespace(
                _devices_inventory_ready=True,
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda _key: False
                ),
                _battery_status_payload={},
                iter_battery_serials=lambda: [],
            )
        )
        == set()
    )
    assert (
        active_battery_serials_for_cleanup(
            SimpleNamespace(
                _devices_inventory_ready=True,
                _selected_type_keys={"encharge"},
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda _key: False,
                    type_bucket=lambda _key: {"count": "bad", "devices": []},
                ),
                iter_battery_serials=lambda: ["BAT-STALE"],
            )
        )
        is None
    )
    assert (
        active_battery_serials_for_cleanup(
            SimpleNamespace(
                _devices_inventory_ready=True,
                _selected_type_keys={"encharge"},
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda _key: False,
                    type_bucket=lambda _key: {"count": 0, "devices": "bad"},
                ),
                iter_battery_serials=lambda: ["BAT-STALE"],
            )
        )
        is None
    )
    assert (
        active_battery_serials_for_cleanup(
            SimpleNamespace(
                _devices_inventory_ready=True,
                _selected_type_keys={"encharge"},
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda _key: False,
                    type_bucket=lambda _key: {"count": 0, "devices": []},
                ),
                iter_battery_serials=lambda: ["BAT-STALE"],
            )
        )
        == set()
    )
    assert active_ac_battery_serials_for_cleanup(
        SimpleNamespace(
            _devices_inventory_ready=True,
            inventory_view=SimpleNamespace(
                has_type_for_entities=lambda key: key == "ac_battery"
            ),
            battery_has_acb=True,
            _ac_battery_devices_payload={},
            iter_ac_battery_serials=lambda: ["ACBAT-1"],
        )
    ) == {"ACBAT-1"}
    assert (
        active_ac_battery_serials_for_cleanup(
            SimpleNamespace(
                _devices_inventory_ready=True,
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda _key: False
                ),
                battery_has_acb=True,
                iter_ac_battery_serials=lambda: ["ACBAT-STALE"],
            )
        )
        is None
    )
    assert (
        active_ac_battery_serials_for_cleanup(
            SimpleNamespace(
                _devices_inventory_ready=True,
                _selected_type_keys={"iqevse"},
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda _key: False
                ),
                battery_has_acb=True,
                iter_ac_battery_serials=lambda: ["ACBAT-STALE"],
            )
        )
        == set()
    )
    assert (
        active_ac_battery_serials_for_cleanup(
            SimpleNamespace(
                _devices_inventory_ready=True,
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda _key: False
                ),
                battery_has_acb=True,
                _ac_battery_devices_payload={},
                iter_ac_battery_serials=lambda: [],
            )
        )
        == set()
    )
    assert active_inverter_serials_for_cleanup(
        SimpleNamespace(
            _devices_inventory_ready=True,
            inventory_view=SimpleNamespace(
                has_type_for_entities=lambda key: key == "microinverter"
            ),
            include_inverters=True,
            _inverters_inventory_payload={},
            iter_inverter_serials=lambda: ["INV-1"],
        )
    ) == {"INV-1"}
    assert (
        active_inverter_serials_for_cleanup(
            SimpleNamespace(
                _devices_inventory_ready=True,
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda _key: False
                ),
                include_inverters=True,
                iter_inverter_serials=lambda: ["INV-STALE"],
            )
        )
        is None
    )
    assert (
        active_inverter_serials_for_cleanup(
            SimpleNamespace(
                _devices_inventory_ready=True,
                _selected_type_keys={"iqevse"},
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda _key: False
                ),
                include_inverters=True,
                iter_inverter_serials=lambda: ["INV-STALE"],
            )
        )
        == set()
    )
    assert (
        active_inverter_serials_for_cleanup(
            SimpleNamespace(
                _devices_inventory_ready=True,
                inventory_view=SimpleNamespace(
                    has_type_for_entities=lambda _key: False
                ),
                include_inverters=True,
                _inverters_inventory_payload={},
                iter_inverter_serials=lambda: [],
            )
        )
        == set()
    )

    coord = SimpleNamespace(
        _devices_inventory_ready=True,
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda key: key == "encharge"
        ),
        _battery_status_payload={},
        iter_serials=lambda: BadIter(),
        iter_battery_serials=lambda: [None, " BAT-1 "],
        iter_ac_battery_serials=lambda: [],
        iter_inverter_serials=lambda: [],
    )

    assert all_active_serial_registry_identifiers(coord) == {"BAT-1"}
    assert (
        prefixed_serial_from_unique_id(
            f"{DOMAIN}_site_{site_id}_battery_overall_status",
            prefix=f"{DOMAIN}_site_{site_id}_battery_",
            suffixes=("_status",),
            blocked_unique_ids={f"{DOMAIN}_site_{site_id}_battery_overall_status"},
        )
        is None
    )
    assert (
        prefixed_serial_from_unique_id(
            f"{DOMAIN}_site_{site_id}_battery_BAT-1_unknown",
            prefix=f"{DOMAIN}_site_{site_id}_battery_",
            suffixes=("_status",),
        )
        is None
    )
    assert (
        _serial_entity_group_from_unique_id(
            f"{DOMAIN}_site_{site_id}_battery_BAT-1_status",
            domain="sensor",
            site_id=BadSite(),
        )
        is None
    )


def test_remove_empty_inactive_serial_devices_handles_guard_paths(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    module = importlib.import_module("custom_components.enphase_ev")
    coord_not_ready = SimpleNamespace(
        _devices_inventory_ready=False,
        iter_serials=lambda: [],
    )
    dev_reg = SimpleNamespace(async_remove_device=Mock())

    assert (
        _remove_empty_inactive_serial_devices(
            hass, config_entry, coord_not_ready, dev_reg, "site-1"
        )
        == 0
    )

    original_er = module.er
    monkeypatch.setattr(module, "er", None)
    coord_ready = SimpleNamespace(
        _devices_inventory_ready=True,
        _selected_type_keys={"iqevse"},
        inventory_view=SimpleNamespace(has_type_for_entities=lambda _key: False),
        iter_serials=lambda: [],
        iter_battery_serials=lambda: [],
        iter_ac_battery_serials=lambda: [],
        iter_inverter_serials=lambda: [],
    )
    assert (
        _remove_empty_inactive_serial_devices(
            hass, config_entry, coord_ready, dev_reg, "site-1"
        )
        == 0
    )
    monkeypatch.setattr(module, "er", original_er)

    ent_reg = SimpleNamespace(entities={})
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: ent_reg,
    )
    assert (
        _remove_empty_inactive_serial_devices(
            hass,
            config_entry,
            coord_ready,
            SimpleNamespace(devices={}),
            "site-1",
        )
        == 0
    )

    class BadIdentifier:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    devices = {
        "foreign-entry": SimpleNamespace(
            id="foreign-entry",
            config_entries={"other-entry"},
            identifiers={(DOMAIN, "EV-FOREIGN")},
        ),
        "no-identifiers": SimpleNamespace(
            id="no-identifiers",
            config_entries={config_entry.entry_id},
            identifiers=set(),
        ),
        "other-domain": SimpleNamespace(
            id="other-domain",
            config_entries={config_entry.entry_id},
            identifiers={("other", "EV-OTHER")},
        ),
        "bad-identifier": SimpleNamespace(
            id="bad-identifier",
            config_entries={config_entry.entry_id},
            identifiers={(DOMAIN, BadIdentifier())},
        ),
        "no-id": SimpleNamespace(
            id=None,
            config_entries={config_entry.entry_id},
            identifiers={(DOMAIN, "EV-NO-ID")},
        ),
        "remove-fails": SimpleNamespace(
            id="remove-fails",
            config_entries={config_entry.entry_id},
            identifiers={(DOMAIN, "EV-REMOVE-FAILS")},
        ),
    }
    dev_reg_failing_remove = SimpleNamespace(
        devices=devices,
        async_remove_device=lambda _device_id: (_ for _ in ()).throw(
            RuntimeError("remove failed")
        ),
    )

    assert (
        _remove_empty_inactive_serial_devices(
            hass, config_entry, coord_ready, dev_reg_failing_remove, "site-1"
        )
        == 0
    )
    monkeypatch.setattr(module, "er", original_er)

    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert (
        _remove_empty_inactive_serial_devices(
            hass, config_entry, coord_ready, dev_reg, "site-1"
        )
        == 0
    )
