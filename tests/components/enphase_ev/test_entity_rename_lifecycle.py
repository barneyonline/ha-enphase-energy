"""Registry renames preserve site-sensor resources and source freshness."""

from datetime import timedelta
import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.enphase_ev.sensor import EnphaseSiteBackoffEndsSensor
from custom_components.enphase_ev.sensor_base import EnphaseSiteSensorEntity
from custom_components.enphase_ev.sensor_site_energy import (
    EnphaseSiteEnergySensor,
    EnphaseGridPowerSensor,
    EnphaseBatteryPowerSensor,
    EnphaseSiteConsumptionPowerSensor,
    EnphaseCurrentPowerConsumptionSensor,
)
from custom_components.enphase_ev.sensor_tariff import EnphaseCurrentTariffRateSensor


@pytest.mark.parametrize(
    "kind",
    [
        "freshness",
        "energy",
        "grid",
        "battery",
        "balance",
        "production",
        "tariff",
        "backoff",
    ],
)
async def test_registry_rename_keeps_one_subscription_and_unloads(
    hass, coordinator_factory, config_entry, monkeypatch, kind
):
    coord = coordinator_factory()
    coord.update_interval = None
    now = [dt_util.utcnow()]
    monkeypatch.setattr(dt_util, "utcnow", lambda: now[0])
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor_base.inventory_type_available",
        lambda *_: True,
    )
    coord.last_success_utc = now[0]
    coord.note_endpoint_family_success("battery_status")
    coord.async_set_updated_data(dict(coord.data))
    factories = {
        "freshness": lambda: EnphaseSiteSensorEntity(
            coord, "rename", "Rename", "encharge"
        ),
        "energy": lambda: EnphaseSiteEnergySensor(
            coord, "grid_import", "grid_import", "Import"
        ),
        "grid": lambda: EnphaseGridPowerSensor(coord),
        "battery": lambda: EnphaseBatteryPowerSensor(coord),
        "balance": lambda: EnphaseSiteConsumptionPowerSensor(coord),
        "production": lambda: EnphaseCurrentPowerConsumptionSensor(coord),
        "tariff": lambda: EnphaseCurrentTariffRateSensor(coord, is_import=True),
        "backoff": lambda: EnphaseSiteBackoffEndsSensor(coord),
    }
    entity = factories[kind]()
    if kind == "freshness":
        entity._attr_device_class = SensorDeviceClass.POWER
        entity._attr_native_value = 75
        entity._attr_native_unit_of_measurement = "W"
    entity.entity_id = "sensor.before_rename"
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    platform = component._platforms["sensor"]
    platform.config_entry = config_entry
    await component.async_add_entities([entity])
    timer = entity._cancel_freshness_expiry
    listener_count = len(coord._listeners)
    observed = entity._source_first_observed
    registry = er.async_get(hass)
    with patch.object(
        entity, "async_added_to_hass", AsyncMock(wraps=entity.async_added_to_hass)
    ) as added, patch.object(
        entity,
        "async_will_remove_from_hass",
        AsyncMock(wraps=entity.async_will_remove_from_hass),
    ) as removed:
        for name in ("sensor.after_rename", "sensor.renamed_again"):
            previous = entity.entity_id
            registry.async_update_entity(previous, new_entity_id=name)
            await hass.async_block_till_done()
            assert hass.states.get(previous) is None
            assert hass.states.get(name) is not None
            assert platform.entities[name] is entity
            assert previous not in platform.entities
            assert len(coord._listeners) == listener_count
            if hasattr(Entity, "async_entity_id_changed"):
                assert entity._cancel_freshness_expiry is timer
                assert entity._source_first_observed == observed
        if hasattr(Entity, "async_entity_id_changed"):
            added.assert_not_awaited()
            removed.assert_not_awaited()
        else:
            assert added.await_count == 2
            assert removed.await_count == 2
            # The new hook is never invoked by old Core; it is also safe to call.
            entity.async_entity_id_changed("sensor.after_rename")
    if kind == "freshness":
        assert hass.states.get(entity.entity_id).state == "75"
        now[0] += timedelta(hours=2)
        async_fire_time_changed(hass, now[0])
        await hass.async_block_till_done()
        assert hass.states.get(entity.entity_id).state == "unavailable"
        assert hass.states.get("sensor.before_rename") is None
        assert hass.states.get("sensor.after_rename") is None
    await component.async_remove_entity(entity.entity_id)
    assert entity._cancel_freshness_expiry is None
    assert len(coord._listeners) == listener_count - 1


async def test_rename_calls_core_hook_without_replacing_timer(
    hass, coordinator_factory, monkeypatch
):
    entity = EnphaseSiteSensorEntity(coordinator_factory(), "rename", "Rename")
    parent = Mock()
    monkeypatch.setattr(Entity, "async_entity_id_changed", parent, raising=False)
    cancel = Mock()
    entity._cancel_freshness_expiry = cancel
    entity.async_entity_id_changed("sensor.old")
    parent.assert_called_once_with("sensor.old")
    assert entity._cancel_freshness_expiry is cancel
    cancel.assert_not_called()
    entity._cancel_freshness_expiry = None
