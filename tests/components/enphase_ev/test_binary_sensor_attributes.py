from types import SimpleNamespace

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers.entity import EntityCategory

from custom_components.enphase_ev.binary_sensor import ConnectedBinarySensor
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def _dummy_coord(payload: dict) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.data = {RANDOM_SERIAL: payload}
    coord.serials = {RANDOM_SERIAL}
    coord.site_id = "site"
    coord.iter_serials = lambda: coord.serials
    coord.async_add_listener = lambda cb, context=None: (lambda: None)
    coord.last_update_success = True
    return coord


def test_connected_binary_sensor_attributes_and_defaults():
    payload = {
        "sn": RANDOM_SERIAL,
        "name": "Garage EV",
        "connected": True,
        "connection": " ethernet ",
        "ip_address": " 192.0.2.10 ",
    }
    sensor = ConnectedBinarySensor(_dummy_coord(payload), RANDOM_SERIAL)
    assert sensor.device_class == BinarySensorDeviceClass.CONNECTIVITY
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor.available

    attrs = sensor.extra_state_attributes
    assert attrs["connection"] == "Ethernet"
    assert attrs["ip_address"] == "192.0.2.10"

    payload["connection"] = None
    payload["ip_address"] = ""
    attrs_blank = sensor.extra_state_attributes
    assert attrs_blank["connection"] is None
    assert attrs_blank["ip_address"] is None

    payload["connected"] = False
    assert sensor.available
    assert not sensor.is_on
    payload["connected"] = None
    assert not sensor.available
    payload.pop("connected")
    assert not sensor.available


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" wifi ", "Wi-Fi"),
        ("WI-FI", "Wi-Fi"),
        (" cellular ", "Cellular"),
        ("Satellite", "Satellite"),
        (42, None),
    ],
)
def test_connected_binary_sensor_connection_method_label(raw, expected):
    sensor = ConnectedBinarySensor(
        _dummy_coord({"connected": True, "connection": raw}), RANDOM_SERIAL
    )
    assert sensor.extra_state_attributes["connection"] == expected
