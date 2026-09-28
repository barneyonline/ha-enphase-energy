"""Regression coverage for battery telemetry and restored device discovery."""

from custom_components.enphase_ev.sensor_battery import (
    EnphaseBatteryStorageChargeSensor,
    EnphaseBatteryStorageStatusSensor,
    EnphaseBatteryStorageHealthSensor,
    EnphaseBatteryStorageCycleCountSensor,
)


def test_iq_battery_live_payload_keeps_all_per_device_sensors_available(
    coordinator_factory,
):
    coord = coordinator_factory(serials=[])
    coord._selected_type_keys = {"encharge"}
    coord._devices_inventory_ready = True
    coord._battery_has_encharge = True
    coord.last_update_success = True
    coord._parse_battery_status_payload(
        {
            "current_charge": "59%",
            "storages": [
                {
                    "id": index,
                    "serial_number": serial,
                    "current_charge": "60%",
                    "available_energy": 3,
                    "max_capacity": 5,
                    "led_status": 13,
                    "excluded": False,
                    "status": "normal",
                    "statusText": "Normal",
                    "cycle_count": 286,
                    "battery_soh": "100%",
                }
                for index, serial in enumerate(("BAT-1", "BAT-2"), 1)
            ],
        }
    )
    serials = list(coord.iter_battery_serials())
    assert set(serials) == {"BAT-1", "BAT-2"}
    for serial in serials:
        for sensor_class, expected in (
            (EnphaseBatteryStorageChargeSensor, 60.0),
            (EnphaseBatteryStorageStatusSensor, "discharging"),
            (EnphaseBatteryStorageHealthSensor, 100.0),
            (EnphaseBatteryStorageCycleCountSensor, 286),
        ):
            sensor = sensor_class(coord, serial)
            assert sensor.available
            assert sensor.native_value == expected


async def test_restored_batteries_are_discovered_when_status_becomes_authoritative(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev import sensor as sensor_mod
    from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
    from custom_components.enphase_ev.reload_snapshot import ReloadSnapshot

    source = coordinator_factory(serials=[])
    source._selected_type_keys = {"encharge"}
    source._battery_has_encharge = True
    source._battery_storage_order = ["BAT-1"]
    source._battery_storage_data = {"BAT-1": {"serial_number": "BAT-1"}}
    coord = coordinator_factory(serials=[])
    ReloadSnapshot.capture(source).apply(coord)
    coord._selected_type_keys = {"encharge"}
    coord._devices_inventory_ready = True
    coord._battery_status_payload = None
    coord._refresh_cached_topology()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    added = []
    await sensor_mod.async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )
    assert not any(
        isinstance(item, EnphaseBatteryStorageChargeSensor) for item in added
    )
    payload = {"storages": [{"serial_number": "BAT-1", "current_charge": "60%"}]}
    coord._battery_status_payload = payload
    coord._parse_battery_status_payload(payload)
    battery_sensors = [
        item for item in added if isinstance(item, EnphaseBatteryStorageChargeSensor)
    ]
    assert len(battery_sensors) == 1
    assert battery_sensors[0].native_value == 60.0
    added_count = len(added)
    coord._parse_battery_status_payload(payload)
    assert len(added) == added_count
    await coord.async_shutdown()


async def test_restored_inverters_are_discovered_when_inventory_becomes_authoritative(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev import sensor as sensor_mod
    from custom_components.enphase_ev.const import (
        OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
        OPT_MICROINVERTER_POWER_ENABLED,
    )
    from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
    from custom_components.enphase_ev.reload_snapshot import ReloadSnapshot

    source = coordinator_factory(serials=[])
    source._selected_type_keys = {"microinverter"}
    source.inventory_runtime._set_type_device_buckets(
        {"microinverter": {"count": 1, "devices": [{"serial_number": "INV-1"}]}},
        ["microinverter"],
    )
    source._inverter_order = ["INV-1"]
    source._inverter_data = {
        "INV-1": {
            "serial_number": "INV-1",
            "lifetime_production_wh": 1000,
            "telemetry": {"power": 100},
        }
    }
    coord = coordinator_factory(serials=[])
    ReloadSnapshot.capture(source).apply(coord)
    coord._selected_type_keys = {"microinverter"}
    coord._devices_inventory_ready = True
    coord._inverters_inventory_payload = None
    coord._refresh_cached_topology()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    hass.config_entries.async_update_entry(
        config_entry,
        options={
            OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED: True,
            OPT_MICROINVERTER_POWER_ENABLED: True,
        },
    )
    added = []
    await sensor_mod.async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )
    inverter_types = (
        sensor_mod.EnphaseInverterLifetimeEnergySensor,
        sensor_mod.EnphaseInverterTelemetrySensor,
    )
    assert not any(isinstance(item, inverter_types) for item in added)
    coord._inverters_inventory_payload = {}
    coord._refresh_cached_topology()
    inverters = [item for item in added if isinstance(item, inverter_types)]
    assert len(inverters) == 1
    assert isinstance(inverters[0], sensor_mod.EnphaseInverterLifetimeEnergySensor)
    coord._inverter_data["INV-1"]["telemetry"] = {"power": 100}
    coord._refresh_cached_topology()
    inverters = [item for item in added if isinstance(item, inverter_types)]
    assert len(inverters) == 2
    added_count = len(added)
    coord._refresh_cached_topology()
    assert len(added) == added_count
    await coord.async_shutdown()
