"""Platform setup remains usable with coordinators without listener extensions."""

from importlib import import_module

import pytest

from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData


@pytest.mark.parametrize(
    "platform", ["binary_sensor", "sensor", "select", "switch", "time", "update"]
)
async def test_platform_setup_without_listener_extensions(
    hass, config_entry, coordinator_factory, monkeypatch, platform
):
    coord = coordinator_factory(serials=[], data={})
    monkeypatch.setattr(coord, "async_add_listener", None)
    monkeypatch.setattr(coord, "async_add_topology_listener", None)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    added = []

    def add_entities(entities, update_before_add=False):
        added.extend(entities)

    if platform == "sensor":
        monkeypatch.setattr(
            "custom_components.enphase_ev.sensor.setup_array_sensors",
            lambda *args: None,
        )
    await import_module(f"custom_components.enphase_ev.{platform}").async_setup_entry(
        hass, config_entry, add_entities
    )
    assert all(entity.unique_id for entity in added)


async def test_switch_setup_without_mapping_payload(
    hass, config_entry, coordinator_factory, monkeypatch
):
    coord = coordinator_factory(serials=[])
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    monkeypatch.setattr(coord, "iter_serials", lambda: [])
    coord.data = ["malformed"]
    try:
        await import_module("custom_components.enphase_ev.switch").async_setup_entry(
            hass, config_entry, lambda entities, **kwargs: None
        )
    finally:
        coord.data = {}


async def test_sensor_setup_before_inventory_is_available(
    hass, config_entry, coordinator_factory, monkeypatch
):
    coord = coordinator_factory(serials=[])
    monkeypatch.setattr(
        coord.inventory_view, "has_type_for_entities", lambda key: False
    )
    coord._type_device_buckets = {}
    coord._devices_inventory_ready = False
    coord._hems_inventory_ready = False
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    await import_module("custom_components.enphase_ev.sensor").async_setup_entry(
        hass, config_entry, lambda entities, **kwargs: None
    )
