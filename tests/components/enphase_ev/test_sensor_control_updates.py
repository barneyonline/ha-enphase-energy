"""Control progress belongs to the device exposing the corresponding controls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.number import EnphaseTariffRateNumber
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.sensor import async_setup_entry
from custom_components.enphase_ev.sensor_control_updates import (
    EnphaseChargerUpdateStatusSensor,
    EnphaseDeviceUpdateStatusSensor,
    tariff_updates_use_cloud_device,
)
from custom_components.enphase_ev.tariff import (
    parse_tariff_rate,
    tariff_rate_sensor_specs,
)


def _seed_devices(coord, types):
    coord.inventory_runtime._set_type_device_buckets(
        {
            key: {
                "type_key": key,
                "count": 1,
                "devices": (
                    [{"serial_number": serial} for serial in sorted(coord.serials)]
                    if key == "iqevse"
                    else [{"serial_number": f"{key}-1"}]
                ),
            }
            for key in types
        },
        types,
    )
    coord._devices_inventory_ready = True


def test_device_status_tracks_only_owned_controls(coordinator_factory):
    coord = coordinator_factory(serials=["EV1"])
    _seed_devices(coord, ["envoy", "encharge", "iqevse"])
    gateway = EnphaseDeviceUpdateStatusSensor(coord, "envoy")
    battery = EnphaseDeviceUpdateStatusSensor(coord, "encharge")
    charger = EnphaseChargerUpdateStatusSensor(coord, "EV1")
    coord.last_update_success = True
    for sensor, device_key in (
        (gateway, "envoy"),
        (battery, "encharge"),
    ):
        assert sensor.entity_category == EntityCategory.DIAGNOSTIC
        assert sensor.translation_key == "update_status"
        assert sensor.available
        assert (
            sensor.unique_id
            == f"{DOMAIN}_site_{coord.site_id}_{device_key}_update_status"
        )
        assert sensor.device_info == coord.inventory_view.type_device_info(device_key)
        assert sensor.extra_state_attributes == {"updates": {}}
    assert charger.entity_category == EntityCategory.DIAGNOSTIC
    assert charger.unique_id == f"{DOMAIN}_EV1_update_status"
    assert charger.device_info["identifiers"] == {(DOMAIN, "EV1")}

    reserve = coord.control_updates.begin(
        "battery_reserve", None, {"reserve": 30}, {"reserve": 20}
    )
    coord.control_updates.finish(("battery_reserve", None), reserve, failed=True)
    profile = coord.control_updates.begin(
        "system_profile", None, {"profile_key": "savings"}, {"profile_key": "backup"}
    )
    grid = coord.control_updates.begin(
        "grid_mode", None, {"mode": "off_grid"}, {"mode": "on_grid"}
    )
    grid.status = "unconfirmed"
    assert battery.native_value == "failed"
    assert gateway.native_value == "pending"
    assert charger.native_value == "idle"
    assert set(gateway.extra_state_attributes["updates"]) == {
        "system_profile",
        "grid_mode",
    }
    assert set(battery.extra_state_attributes["updates"]) == {"battery_reserve"}

    coord.control_updates.finish(("system_profile", None), profile)
    coord.control_updates.observe(
        "system_profile",
        None,
        {"profile_key": "savings"},
        coord.control_updates.read_tokens(),
    )
    assert gateway.native_value == "unconfirmed"
    assert (
        gateway.extra_state_attributes["updates"]["system_profile"]["status"]
        == "confirmed"
    )
    assert gateway.extra_state_attributes["updates"]["system_profile"]["confirmed"] == {
        "profile_key": "savings"
    }
    # Attributes are detached from runtime state.
    gateway.extra_state_attributes["updates"]["system_profile"]["requested"][
        "profile_key"
    ] = "other"
    assert profile.requested == {"profile_key": "savings"}


def test_gateway_includes_system_controller_progress(coordinator_factory):
    coord = coordinator_factory()
    _seed_devices(coord, ["envoy", "encharge"])
    gateway = EnphaseDeviceUpdateStatusSensor(coord, "envoy")
    coord.control_updates.begin("grid_mode", None, {"mode": "off_grid"}, {})
    assert gateway.native_value == "pending"
    assert set(gateway.extra_state_attributes["updates"]) == {"grid_mode"}
    assert EnphaseDeviceUpdateStatusSensor(coord, "encharge").native_value == "idle"


def test_tariff_routing_does_not_require_optional_gateway_metadata(coordinator_factory):
    coord = coordinator_factory()

    def fail_metadata(_key):
        raise RuntimeError("Optional gateway metadata unavailable")

    coord.inventory_view.type_bucket = fail_metadata
    coord.control_updates.begin("tariff", None, {"payload": {}}, {})
    assert not tariff_updates_use_cloud_device(coord)
    assert EnphaseDeviceUpdateStatusSensor(coord, "envoy").native_value == "pending"
    coord.inventory_view = SimpleNamespace(type_device_info=lambda _key: None)
    assert tariff_updates_use_cloud_device(coord)


def test_shared_storm_evse_progress_is_visible_on_each_charger(coordinator_factory):
    coord = coordinator_factory(serials=["EV1", "EV2"])
    chargers = [
        EnphaseChargerUpdateStatusSensor(coord, serial) for serial in ("EV1", "EV2")
    ]
    coord.control_updates.begin(
        "storm_evse", None, {"enabled": True}, {"enabled": False}
    )
    for charger in chargers:
        assert charger.native_value == "pending"
        assert charger.extra_state_attributes["updates"]["storm_evse"]["confirmed"] == {
            "enabled": False
        }
    assert EnphaseDeviceUpdateStatusSensor(coord, "envoy").native_value == "idle"
    assert EnphaseDeviceUpdateStatusSensor(coord, "encharge").native_value == "idle"


def test_gateway_export_and_profile_confirmation(coordinator_factory):
    coord = coordinator_factory()
    sensor = EnphaseDeviceUpdateStatusSensor(coord, "envoy")
    coord.export_limit_runtime = SimpleNamespace(
        enabled=True,
        pending=None,
        request_status="confirmed",
        attributes=lambda: {"confirmed_watts": 500},
    )
    assert sensor.native_value == "idle"
    assert (
        sensor.extra_state_attributes["updates"]["export_limit"]["confirmed_watts"]
        == 500
    )
    coord.grid_profile_runtime.pending_profile_id = "new"
    coord.control_updates.begin(
        "grid_profile", None, {"profile_id": "new"}, {"profile_id": "old"}
    )
    assert sensor.native_value == "pending"
    # Tracked progress takes precedence over the legacy grid-profile fallback.
    assert sensor.extra_state_attributes["updates"]["grid_profile"]["requested"] == {
        "profile_id": "new"
    }


@pytest.mark.asyncio
async def test_charger_progress_remains_available_after_cloud_refresh_failure(
    coordinator_factory,
):
    """A failed telemetry read must not hide locally tracked command progress."""
    coord = coordinator_factory(serials=["EV1"])
    coord.last_success_utc = dt_util.utcnow()
    sensor = EnphaseChargerUpdateStatusSensor(coord, "EV1")
    update = coord.control_updates.begin(
        "charge_mode", "EV1", {"mode": "SMART_CHARGING"}, {"mode": "IMMEDIATE"}
    )
    coord._async_update_data = AsyncMock(side_effect=UpdateFailed("Cloud unavailable"))

    await coord.async_refresh()

    assert coord.last_update_success is False
    assert sensor.native_value == "pending"
    assert sensor.available
    # The diagnostic remains useful when confirmation expires or a write fails.
    for status in ("unconfirmed", "failed"):
        update.status = status
        assert sensor.native_value == status
        assert sensor.available
    assert not EnphaseChargerUpdateStatusSensor(coord, "MISSING").available


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway_selected", [False, True])
async def test_tariff_progress_follows_cloud_controls_without_selected_gateway(
    hass,
    config_entry,
    coordinator_factory,
    gateway_selected,
):
    """Cloud tariff controls still need progress when gateway entities are excluded."""
    coord = coordinator_factory(serials=["EV1"])
    coord.config_entry = config_entry
    if gateway_selected:
        _seed_devices(coord, ["iqevse"])
        coord._selected_type_keys = ["envoy", "iqevse"]
        coord._devices_inventory_ready = False
    else:
        _seed_devices(coord, ["envoy", "iqevse"])
        coord._selected_type_keys = ["iqevse"]
    coord.tariff_import_rate = parse_tariff_rate(
        {
            "purchase": {
                "typeKind": "single",
                "typeId": "flat",
                "seasons": [
                    {
                        "id": "default",
                        "days": [
                            {
                                "id": "week",
                                "periods": [{"id": "off-peak", "rate": "0.18"}],
                            }
                        ],
                    }
                ],
            }
        },
        "purchase",
    )
    tariff = EnphaseTariffRateNumber(
        coord,
        tariff_rate_sensor_specs(coord.tariff_import_rate)[0],
        is_import=True,
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    coord.control_updates.begin("tariff", None, {"payload": {"rate": "0.2"}}, {})
    callbacks = []
    coord.async_add_topology_listener = lambda callback: (
        callbacks.append(callback) or (lambda: None)
    )
    added = []
    await async_setup_entry(
        hass, config_entry, lambda entities, **kwargs: added.extend(entities)
    )
    status = next(
        entity
        for entity in added
        if entity.translation_key == "update_status"
        and entity.device_info == tariff.device_info
    )
    assert status.entity_category == EntityCategory.DIAGNOSTIC
    assert status.native_value == "pending"
    assert set(status.extra_state_attributes["updates"]) == {"tariff"}
    if gateway_selected:
        gateway = next(
            entity
            for entity in added
            if entity.unique_id.endswith("envoy_update_status")
        )
        assert gateway.native_value == "idle"
    # Discovery/reconfiguration must move progress back with the tariff controls.
    registry = er.async_get(hass)
    cloud_entry = registry.async_get_or_create(
        "sensor", DOMAIN, status.unique_id, config_entry=config_entry
    )
    coord._selected_type_keys = ["envoy", "iqevse"]
    _seed_devices(coord, ["envoy", "iqevse"])
    sync = next(
        callback
        for callback in callbacks
        if callback.__name__ == "_async_sync_topology"
    )
    sync()
    assert registry.async_get(cloud_entry.entity_id) is None
    gateway = next(
        entity for entity in added if entity.unique_id.endswith("envoy_update_status")
    )
    assert gateway.device_info == tariff.device_info
    assert gateway.native_value == "pending"


@pytest.mark.asyncio
async def test_first_tariff_service_update_creates_cloud_diagnostic(
    hass, config_entry, coordinator_factory
):
    """A service can submit a tariff before rate metadata has been discovered."""
    coord = coordinator_factory(serials=["EV1"])
    _seed_devices(coord, ["iqevse"])
    coord._selected_type_keys = ["iqevse"]
    coord.config_entry = config_entry
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    added = []
    await async_setup_entry(
        hass, config_entry, lambda entities, **kwargs: added.extend(entities)
    )
    assert not any(entity.unique_id.endswith("cloud_update_status") for entity in added)

    coord.control_updates.begin("tariff", None, {"payload": {"rate": "0.2"}}, {})

    status = next(
        entity for entity in added if entity.unique_id.endswith("cloud_update_status")
    )
    assert status.native_value == "pending"


@pytest.mark.parametrize("last_update_success", [False, True])
def test_charger_diagnostic_without_success_history(
    coordinator_factory, last_update_success
):
    coord = coordinator_factory(serials=["EV1"])
    coord.last_success_utc = None
    coord.last_update_success = last_update_success
    assert (
        EnphaseChargerUpdateStatusSensor(coord, "EV1").available is last_update_success
    )


@pytest.mark.asyncio
async def test_setup_retires_central_sensor_and_prunes_device_status(
    hass, config_entry, coordinator_factory
):
    coord = coordinator_factory(serials=["EV1", "EV2"])
    coord.config_entry = config_entry
    coord._battery_has_encharge = True
    _seed_devices(coord, ["envoy", "encharge", "iqevse"])
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    registry = er.async_get(hass)
    central = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_control_update_status",
        config_entry=config_entry,
    )
    unrelated = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_other_control_update_status",
        config_entry=config_entry,
    )
    callbacks = []
    coord.async_add_topology_listener = lambda callback: (
        callbacks.append(callback) or (lambda: None)
    )
    added = []
    await async_setup_entry(
        hass, config_entry, lambda entities, **kwargs: added.extend(entities)
    )
    assert registry.async_get(central.entity_id) is None
    assert registry.async_get(unrelated.entity_id) is not None
    status = [entity for entity in added if entity.translation_key == "update_status"]
    assert len(status) == 4
    assert all(entity.entity_category == EntityCategory.DIAGNOSTIC for entity in status)
    assert not any(
        entity.translation_key == "control_update_status" for entity in added
    )
    entries = {
        entity.unique_id: registry.async_get_or_create(
            "sensor", DOMAIN, entity.unique_id, config_entry=config_entry
        )
        for entity in status
    }
    sync = next(
        callback
        for callback in callbacks
        if callback.__name__ == "_async_sync_topology"
    )
    sync()
    assert (
        len([entity for entity in added if entity.translation_key == "update_status"])
        == 4
    )
    coord.serials.remove("EV2")
    coord.data.pop("EV2")
    coord._battery_has_encharge = False
    _seed_devices(coord, ["envoy", "iqevse"])
    sync()
    for unique_id, entry in entries.items():
        if (
            unique_id.endswith("encharge_update_status")
            or unique_id == f"{DOMAIN}_EV2_update_status"
        ):
            assert registry.async_get(entry.entity_id) is None
        else:
            assert registry.async_get(entry.entity_id) is not None
