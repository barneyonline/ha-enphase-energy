from types import SimpleNamespace

from tests.components.enphase_ev.random_ids import (
    RANDOM_SERIAL,
    RANDOM_SERIAL_ALT,
    RANDOM_SITE_ID,
)


def _with_inventory_view(coord):
    coord.inventory_view = SimpleNamespace(type_identifier=lambda _type_key: None)
    return coord


def test_entity_naming_and_availability():
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    class DummyCoord:
        def __init__(self):
            self.data = {}
            self.serials = {RANDOM_SERIAL}
            self.site_id = RANDOM_SITE_ID
            self.last_update_success = True

    coord = _with_inventory_view(DummyCoord())
    coord.data = {
        RANDOM_SERIAL: {
            "sn": RANDOM_SERIAL,
            "name": "Garage EV",
            "connected": True,
            "plugged": True,
            "charging": False,
            "faulted": False,
            "connector_status": "AVAILABLE",
            "lifetime_kwh": 0.0,
            "session_start": None,
        }
    }

    ent = EnphaseEnergyTodaySensor(coord, RANDOM_SERIAL)
    assert ent.available is True
    # Uses has_entity_name with a translation key; the display name now comes
    # from the translations instead of a hardcoded _attr_name.
    assert ent.has_entity_name is True
    assert ent.translation_key == "last_session"
    # Device name comes from coordinator data
    assert ent.device_info["name"] == "Garage EV"
    # Unique ID includes domain, serial, and key
    assert ent.unique_id.endswith(f"{RANDOM_SERIAL}_energy_today")


def test_connector_status_uses_translated_entity_name():
    """Connector status should not override its translated entity name."""
    from custom_components.enphase_ev.sensor import EnphaseConnectorStatusSensor

    coord = _with_inventory_view(
        SimpleNamespace(
            data={RANDOM_SERIAL: {"connector_status": "AVAILABLE"}},
            serials={RANDOM_SERIAL},
            site_id=RANDOM_SITE_ID,
            last_update_success=True,
        )
    )

    entity = EnphaseConnectorStatusSensor(coord, RANDOM_SERIAL)
    translation_key = "component.enphase_ev.entity.sensor.connector_status.name"
    entity.platform_data = SimpleNamespace(
        platform_name="enphase_ev",
        domain="sensor",
        platform_translations={translation_key: "Translated connector status"},
        component_translations={},
    )

    assert "_attr_name" not in entity.__dict__
    assert entity.translation_key == "connector_status"
    assert entity.name == "Translated connector status"


def test_inverter_lifetime_energy_uses_translated_name_with_serial(
    coordinator_factory,
):
    """Per-inverter names should use a translated serial placeholder."""
    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    translation_key = "component.enphase_ev.entity.sensor.inverter_lifetime_energy.name"
    entity.platform_data = SimpleNamespace(
        platform_name="enphase_ev",
        domain="sensor",
        platform_translations={translation_key: "{serial} Translated lifetime energy"},
        component_translations={},
    )

    assert "_attr_name" not in entity.__dict__
    assert entity.translation_key == "inverter_lifetime_energy"
    assert entity.translation_placeholders == {"serial": "INV-A"}
    assert entity.name == "INV-A Translated lifetime energy"


def test_device_info_includes_model_name_when_available():
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    class DummyCoord:
        def __init__(self):
            self.data = {}
            self.serials = {RANDOM_SERIAL_ALT}
            self.site_id = RANDOM_SITE_ID
            self.last_update_success = True

    coord = _with_inventory_view(DummyCoord())
    coord.data = {
        RANDOM_SERIAL_ALT: {
            "sn": RANDOM_SERIAL_ALT,
            "display_name": "IQ EV Charger",
            "model_name": "IQ-EVSE-EU-3032",
            "connected": True,
        }
    }

    ent = EnphaseEnergyTodaySensor(coord, RANDOM_SERIAL_ALT)
    info = ent.device_info
    assert info["name"] == "IQ EV Charger"
    assert info["model"] == "IQ EV Charger (IQ-EVSE-EU-3032)"


def test_device_info_suppresses_duplicate_extended_evse_model_suffix():
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    class DummyCoord:
        def __init__(self):
            self.data = {}
            self.serials = {RANDOM_SERIAL_ALT}
            self.site_id = RANDOM_SITE_ID
            self.last_update_success = True

    coord = _with_inventory_view(DummyCoord())
    coord.data = {
        RANDOM_SERIAL_ALT: {
            "sn": RANDOM_SERIAL_ALT,
            "display_name": "IQ EV Charger (IQ-EVSE-EU-3032)",
            "model_name": "IQ-EVSE-EU-3032-0105-1300",
            "connected": True,
        }
    }

    ent = EnphaseEnergyTodaySensor(coord, RANDOM_SERIAL_ALT)
    info = ent.device_info
    assert info["name"] == "IQ EV Charger (IQ-EVSE-EU-3032)"
    assert info["model"] == "IQ EV Charger (IQ-EVSE-EU-3032)"


def test_device_info_handles_empty_or_invalid_model_name():
    from custom_components.enphase_ev.sensor import EnphaseEnergyTodaySensor

    class _BadStr:
        def __str__(self):
            raise ValueError("boom")

    class DummyCoord:
        def __init__(self):
            self.data = {}
            self.serials = {RANDOM_SERIAL_ALT}
            self.site_id = RANDOM_SITE_ID
            self.last_update_success = True

    coord = _with_inventory_view(DummyCoord())
    coord.data = {
        RANDOM_SERIAL_ALT: {
            "sn": RANDOM_SERIAL_ALT,
            "display_name": "Garage Charger",
            "model_name": "   ",
            "connected": True,
        }
    }
    ent = EnphaseEnergyTodaySensor(coord, RANDOM_SERIAL_ALT)
    assert ent.device_info["model"] == "Garage Charger"

    coord.data[RANDOM_SERIAL_ALT]["model_name"] = _BadStr()
    assert ent.device_info["model"] == "Garage Charger"
