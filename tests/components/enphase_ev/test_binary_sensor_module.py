from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable
from unittest.mock import MagicMock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

from custom_components.enphase_ev import DOMAIN
from custom_components.enphase_ev import binary_sensor
from custom_components.enphase_ev.binary_sensor import (
    ChargingBinarySensor,
    ConnectedBinarySensor,
    HeatPumpSgReadyActiveBinarySensor,
    PluggedInBinarySensor,
    SiteActiveSystemEventsBinarySensor,
    SiteCloudReachableBinarySensor,
    async_setup_entry,
)
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def _stub_listener() -> Callable[[], None]:
    """Return a reusable unsubscribe stub for coordinator listeners."""
    return lambda: None


@pytest.mark.asyncio
async def test_async_setup_entry_syncs_binary_sensors(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    """Ensure charger binary sensors add once per serial and register unload."""
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": True,
                "charging": False,
                "faulted": False,
                "connected": True,
                "commissioned": True,
            }
        }
    )
    coord.system_events_runtime._last_success_utc = dt_util.utcnow()  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    callbacks: list[Callable[[], None]] = []

    def _capture_listener(callback: Callable[[], None]) -> Callable[[], None]:
        callbacks.append(callback)
        return _stub_listener()

    monkeypatch.setattr(coord, "async_add_topology_listener", _capture_listener)

    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)

    assert (
        len([ent for ent in added if isinstance(ent, SiteCloudReachableBinarySensor)])
        == 1
    )
    assert (
        len(
            [
                ent
                for ent in added
                if isinstance(ent, SiteActiveSystemEventsBinarySensor)
            ]
        )
        == 1
    )
    per_serial = [ent for ent in added if hasattr(ent, "_sn")]
    assert len(per_serial) == 3
    assert {type(ent) for ent in per_serial} == {
        PluggedInBinarySensor,
        ChargingBinarySensor,
        ConnectedBinarySensor,
    }
    assert config_entry._on_unload and callable(config_entry._on_unload[0])

    sync_cb = next(cb for cb in callbacks if cb.__name__ == "_async_sync_chargers")

    sync_cb()
    assert len(added) == 5

    new_serial = "EV0002"
    coord.data[new_serial] = {
        "sn": new_serial,
        "name": "Second EV",
        "plugged": False,
        "charging": True,
        "faulted": False,
        "connected": True,
        "commissioned": False,
    }
    coord._ensure_serial_tracked(new_serial)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_label": "EV Chargers",
                "count": 2,
                "devices": [
                    {"serial_number": RANDOM_SERIAL},
                    {"serial_number": new_serial},
                ],
            }
        },
        ["iqevse"],
    )

    sync_cb()
    assert len(added) == 8
    assert {ent._sn for ent in added if hasattr(ent, "_sn")} == {
        RANDOM_SERIAL,
        new_serial,
    }

    sync_cb()
    assert len(added) == 8


@pytest.mark.asyncio
async def test_system_events_entity_is_retained_while_reload_fetch_is_pending(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    """Retain the installer-only entity until the reload fetch completes."""
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory(serials=[])
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    topology_callbacks: list[Callable[[], None]] = []
    update_callbacks: list[Callable[[], None]] = []

    def _capture_topology_listener(
        callback: Callable[[], None],
    ) -> Callable[[], None]:
        topology_callbacks.append(callback)
        return _stub_listener()

    def _capture_update_listener(
        callback: Callable[[], None], *, context=None
    ) -> Callable[[], None]:
        update_callbacks.append(callback)
        return _stub_listener()

    monkeypatch.setattr(
        coord, "async_add_topology_listener", _capture_topology_listener
    )
    monkeypatch.setattr(coord, "async_add_listener", _capture_update_listener)

    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_active_system_events",
        config_entry=config_entry,
    )
    added: list[object] = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert len(topology_callbacks) == 1
    assert ent_reg.async_get(stale.entity_id) is not None
    assert (
        len(
            [
                entity
                for entity in added
                if isinstance(entity, SiteCloudReachableBinarySensor)
            ]
        )
        == 1
    )
    event_entities = [
        entity
        for entity in added
        if isinstance(entity, SiteActiveSystemEventsBinarySensor)
    ]
    assert len(event_entities) == 1
    assert event_entities[0].available is False

    coord.system_events_runtime._last_success_utc = dt_util.utcnow()  # noqa: SLF001
    sync_events = next(
        callback
        for callback in update_callbacks
        if callback.__name__ == "_async_sync_system_events"
    )
    sync_events()
    sync_events()

    assert (
        len(
            [
                entity
                for entity in added
                if isinstance(entity, SiteActiveSystemEventsBinarySensor)
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_system_events_entity_is_removed_after_endpoint_suppression(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    """Remove a retained entity only after the optional endpoint is suppressed."""
    from homeassistant.helpers import entity_registry as er

    coord = coordinator_factory(serials=[])
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    callbacks: list[Callable[[], None]] = []

    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    def _capture_update_listener(
        callback: Callable[[], None], *, context=None
    ) -> Callable[[], None]:
        callbacks.append(callback)
        return _stub_listener()

    monkeypatch.setattr(coord, "async_add_listener", _capture_update_listener)
    ent_reg = er.async_get(hass)
    stale = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_active_system_events",
        config_entry=config_entry,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert ent_reg.async_get(stale.entity_id) is not None
    coord._endpoint_family_state("system_events").support_state = "suppressed"
    sync_events = next(
        callback
        for callback in callbacks
        if callback.__name__ == "_async_sync_system_events"
    )
    sync_events()

    assert ent_reg.async_get(stale.entity_id) is None


@pytest.mark.asyncio
async def test_system_events_entity_waits_for_first_success_on_initial_setup(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    """Do not create the installer-only entity before its first success."""
    coord = coordinator_factory(serials=[])
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )
    added: list[object] = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert not any(
        isinstance(entity, SiteActiveSystemEventsBinarySensor) for entity in added
    )


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_historical_charger_binary_sensor_entities(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    fake_registry = SimpleNamespace(
        entities={
            "binary_sensor.old_commissioned": SimpleNamespace(
                entity_id="binary_sensor.old_commissioned",
                domain="binary_sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_commissioned",
                config_entry_id=config_entry.entry_id,
            ),
            "binary_sensor.old_problem": SimpleNamespace(
                entity_id="binary_sensor.old_problem",
                domain="binary_sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_charger_problem",
                config_entry_id=config_entry.entry_id,
            ),
            "binary_sensor.keep_connected": SimpleNamespace(
                entity_id="binary_sensor.keep_connected",
                domain="binary_sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_connected",
                config_entry_id=config_entry.entry_id,
            ),
        },
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.binary_sensor.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert fake_registry.async_remove.call_count == 2
    fake_registry.async_remove.assert_any_call("binary_sensor.old_commissioned")
    fake_registry.async_remove.assert_any_call("binary_sensor.old_problem")


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_removed_charger_binary_sensor_entities(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=["EVKEEP"])
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    fake_registry = SimpleNamespace(
        entities={
            "binary_sensor.old_plugged": SimpleNamespace(
                entity_id="binary_sensor.old_plugged",
                domain="binary_sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_EVOLD_plugged",
                config_entry_id=config_entry.entry_id,
            ),
            "binary_sensor.keep_connected": SimpleNamespace(
                entity_id="binary_sensor.keep_connected",
                domain="binary_sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_EVKEEP_connected",
                config_entry_id=config_entry.entry_id,
            ),
            "binary_sensor.keep_site_status": SimpleNamespace(
                entity_id="binary_sensor.keep_site_status",
                domain="binary_sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_site_{coord.site_id}_connected",
                config_entry_id=config_entry.entry_id,
            ),
            "binary_sensor.ignore_wrong_entry": SimpleNamespace(
                entity_id="binary_sensor.ignore_wrong_entry",
                domain="binary_sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_EVOTHER_plugged",
                config_entry_id="other-entry",
            ),
        },
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.binary_sensor.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_remove.assert_called_once_with("binary_sensor.old_plugged")


@pytest.mark.asyncio
async def test_async_setup_entry_waits_for_authoritative_charger_inventory_before_binary_prune(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=["EVKEEP"])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._active_inventory_evse_serials = lambda: None  # type: ignore[method-assign]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    fake_registry = SimpleNamespace(
        entities={
            "binary_sensor.old_plugged": SimpleNamespace(
                entity_id="binary_sensor.old_plugged",
                domain="binary_sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_EVOLD_plugged",
                config_entry_id=config_entry.entry_id,
            ),
        },
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.binary_sensor.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_remove.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_historical_binary_prune_skips_nonmatching_entries(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    fake_registry = SimpleNamespace(
        entities={
            "binary_sensor.derived_wrong_domain": SimpleNamespace(
                entity_id="sensor.derived_wrong_domain",
                domain=None,
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_commissioned",
                config_entry_id=config_entry.entry_id,
            ),
            "binary_sensor.wrong_platform": SimpleNamespace(
                entity_id="binary_sensor.wrong_platform",
                domain="binary_sensor",
                platform="other_domain",
                unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_commissioned",
                config_entry_id=config_entry.entry_id,
            ),
            "binary_sensor.wrong_entry": SimpleNamespace(
                entity_id="binary_sensor.wrong_entry",
                domain="binary_sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_charger_problem",
                config_entry_id="other-entry-id",
            ),
            "binary_sensor.invalid_unique": SimpleNamespace(
                entity_id="binary_sensor.invalid_unique",
                domain="binary_sensor",
                platform=DOMAIN,
                unique_id=object(),
                config_entry_id=config_entry.entry_id,
            ),
            "binary_sensor.bad_prefix": SimpleNamespace(
                entity_id="binary_sensor.bad_prefix",
                domain="binary_sensor",
                platform=DOMAIN,
                unique_id=f"other_{RANDOM_SERIAL}_commissioned",
                config_entry_id=config_entry.entry_id,
            ),
            "binary_sensor.keep_connected": SimpleNamespace(
                entity_id="binary_sensor.keep_connected",
                domain="binary_sensor",
                platform=DOMAIN,
                unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_connected",
                config_entry_id=config_entry.entry_id,
            ),
        },
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.binary_sensor.er.async_get",
        lambda _hass: fake_registry,
    )
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_remove.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_falls_back_to_generic_listener_for_binary_sensors(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    callbacks: list[Callable[[], None]] = []

    monkeypatch.setattr(coord, "async_add_topology_listener", None, raising=False)
    monkeypatch.setattr(
        coord,
        "async_add_listener",
        lambda callback: callbacks.append(callback) or _stub_listener(),
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert callbacks


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_site_sensor_without_gateway_type(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": True,
                "charging": False,
                "faulted": False,
                "connected": True,
                "commissioned": True,
            }
        }
    )
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": RANDOM_SERIAL, "name": "Garage EV"}],
            }
        },
        ["iqevse"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )
    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)

    assert any(isinstance(ent, SiteCloudReachableBinarySensor) for ent in added)
    assert len([ent for ent in added if hasattr(ent, "_sn")]) == 3


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_site_sensor_when_inventory_unknown(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": True,
                "charging": False,
                "faulted": False,
                "connected": True,
                "commissioned": True,
            }
        }
    )
    coord._type_device_buckets = {}  # noqa: SLF001
    coord._type_device_order = []  # noqa: SLF001
    coord._devices_inventory_ready = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )
    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)

    assert any(isinstance(ent, SiteCloudReachableBinarySensor) for ent in added)
    assert len([ent for ent in added if hasattr(ent, "_sn")]) == 3
    assert not any(isinstance(ent, HeatPumpSgReadyActiveBinarySensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_skips_heatpump_sg_ready_binary_without_dedicated_controller(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG-1",
                        "name": "SG Ready Gateway",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-EM-1",
                        "name": "Energy Meter",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )
    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)

    assert any(isinstance(ent, SiteCloudReachableBinarySensor) for ent in added)
    assert not any(isinstance(ent, HeatPumpSgReadyActiveBinarySensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_does_not_duplicate_site_sensor_when_gateway_type_appears_later(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": True,
                "charging": False,
                "faulted": False,
                "connected": True,
                "commissioned": True,
            }
        }
    )
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": RANDOM_SERIAL, "name": "Garage EV"}],
            }
        },
        ["iqevse"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    callbacks: list[Callable[[], None]] = []

    def _capture_listener(callback: Callable[[], None]) -> Callable[[], None]:
        callbacks.append(callback)
        return _stub_listener()

    monkeypatch.setattr(coord, "async_add_topology_listener", _capture_listener)
    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)
    assert (
        len([ent for ent in added if isinstance(ent, SiteCloudReachableBinarySensor)])
        == 1
    )

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": RANDOM_SERIAL, "name": "Garage EV"}],
            },
        },
        ["envoy", "iqevse"],
    )
    callbacks[0]()

    assert (
        len([ent for ent in added if isinstance(ent, SiteCloudReachableBinarySensor)])
        == 1
    )


@pytest.mark.asyncio
async def test_async_setup_entry_adds_heatpump_sg_ready_binary_sensor_when_type_appears(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": True,
                "charging": False,
                "faulted": False,
                "connected": True,
                "commissioned": True,
            }
        }
    )
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": RANDOM_SERIAL, "name": "Garage EV"}],
            },
        },
        ["envoy", "iqevse"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    callbacks: list[Callable[[], None]] = []

    def _capture_listener(callback: Callable[[], None]) -> Callable[[], None]:
        callbacks.append(callback)
        return _stub_listener()

    monkeypatch.setattr(coord, "async_add_topology_listener", _capture_listener)
    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)
    assert not any(isinstance(ent, HeatPumpSgReadyActiveBinarySensor) for ent in added)

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": RANDOM_SERIAL, "name": "Garage EV"}],
            },
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "name": "Heat Pump",
                    }
                ],
            },
        },
        ["envoy", "iqevse", "heatpump"],
    )
    callbacks[0]()

    assert (
        len(
            [ent for ent in added if isinstance(ent, HeatPumpSgReadyActiveBinarySensor)]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_heatpump_sg_ready_binary_sensor_when_type_removed(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "name": "Heat Pump",
                    }
                ],
            },
        },
        ["envoy", "heatpump"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    callbacks: list[Callable[[], None]] = []

    def _capture_listener(callback: Callable[[], None]) -> Callable[[], None]:
        callbacks.append(callback)
        return _stub_listener()

    fake_registry = SimpleNamespace(
        async_get_entity_id=MagicMock(
            side_effect=lambda _domain, _platform, unique_id: (
                "binary_sensor.heat_pump_sg_ready_active"
                if unique_id.endswith("heat_pump_sg_ready_active")
                else None
            )
        ),
        async_remove=MagicMock(),
    )
    monkeypatch.setattr(coord, "async_add_topology_listener", _capture_listener)
    monkeypatch.setattr(
        "custom_components.enphase_ev.binary_sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)
    assert any(isinstance(ent, HeatPumpSgReadyActiveBinarySensor) for ent in added)

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            }
        },
        ["envoy"],
    )
    callbacks[0]()

    fake_registry.async_get_entity_id.assert_any_call(
        "binary_sensor",
        DOMAIN,
        f"{DOMAIN}_site_{coord.site_id}_heat_pump_sg_ready_active",
    )
    fake_registry.async_remove.assert_called_once_with(
        "binary_sensor.heat_pump_sg_ready_active"
    )


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_heatpump_sg_ready_binary_sensor_when_dedicated_controller_removed(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 3,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "name": "Heat Pump",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-EM-1",
                        "name": "Energy Meter",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG-1",
                        "name": "SG Ready Gateway",
                    },
                ],
            },
        },
        ["envoy", "heatpump"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    callbacks: list[Callable[[], None]] = []

    def _capture_listener(callback: Callable[[], None]) -> Callable[[], None]:
        callbacks.append(callback)
        return _stub_listener()

    fake_registry = SimpleNamespace(
        async_get_entity_id=MagicMock(
            side_effect=lambda _domain, _platform, unique_id: (
                "binary_sensor.heat_pump_sg_ready_active"
                if unique_id.endswith("heat_pump_sg_ready_active")
                else None
            )
        ),
        async_remove=MagicMock(),
    )
    monkeypatch.setattr(coord, "async_add_topology_listener", _capture_listener)
    monkeypatch.setattr(
        "custom_components.enphase_ev.binary_sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    added = []

    def _collect(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _collect)
    assert any(isinstance(ent, HeatPumpSgReadyActiveBinarySensor) for ent in added)

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 2,
                "devices": [
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-EM-1",
                        "name": "Energy Meter",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG-1",
                        "name": "SG Ready Gateway",
                    },
                ],
            },
        },
        ["envoy", "heatpump"],
    )
    callbacks[0]()

    fake_registry.async_remove.assert_called_once_with(
        "binary_sensor.heat_pump_sg_ready_active"
    )


def test_ev_bool_sensors_reflect_coordinator_state(
    coordinator_factory, monkeypatch
) -> None:
    """Validate EV charger binary sensor helpers read from coordinator data."""
    coord = coordinator_factory(
        data={
            RANDOM_SERIAL: {
                "sn": RANDOM_SERIAL,
                "name": "Garage EV",
                "plugged": 0,
                "charging": 1,
                "faulted": True,
                "connected": False,
                "commissioned": True,
                "connection": " wifi ",
                "ip_address": " 192.0.2.10 ",
            }
        }
    )
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    plugged = PluggedInBinarySensor(coord, RANDOM_SERIAL)
    assert plugged.is_on is False
    assert plugged.device_class == BinarySensorDeviceClass.PLUG

    charging = ChargingBinarySensor(coord, RANDOM_SERIAL)
    assert charging.device_class == BinarySensorDeviceClass.BATTERY_CHARGING
    assert charging.icon == "mdi:flash"

    coord.data[RANDOM_SERIAL]["charging"] = 0
    assert charging.icon == "mdi:flash-off"

    connected = ConnectedBinarySensor(coord, RANDOM_SERIAL)
    assert connected.device_class == BinarySensorDeviceClass.CONNECTIVITY
    assert connected.entity_category == EntityCategory.DIAGNOSTIC
    attrs = connected.extra_state_attributes
    assert attrs["connection"] == "wifi"
    assert attrs["ip_address"] == "192.0.2.10"


def test_site_cloud_reachable_binary_sensor_metadata(
    coordinator_factory, monkeypatch
) -> None:
    """Exercise availability, attributes, and device info for the site sensor."""
    coord = coordinator_factory(serials=[], data={})
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    sensor = SiteCloudReachableBinarySensor(coord)
    assert sensor.translation_key == "cloud_reachable"
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC

    coord.last_success_utc = None
    coord.last_update_success = False
    assert sensor.available is False

    now = datetime.now(timezone.utc)
    coord.last_update_success = True
    coord.last_success_utc = now - timedelta(seconds=45)
    coord.update_interval = None
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    assert sensor.available is True
    assert sensor.is_on is True

    monkeypatch.setattr(dt_util, "utcnow", lambda: now + timedelta(seconds=61))
    assert sensor.is_on is False

    failure_time = now - timedelta(seconds=15)
    coord.last_success_utc = now
    coord.last_failure_utc = failure_time
    coord.last_failure_status = 503
    coord.last_failure_description = "Gateway error"
    coord.last_failure_response = {"retry": True}
    coord.last_failure_source = "http"
    coord.backoff_ends_utc = now + timedelta(seconds=90)

    attrs = sensor.extra_state_attributes
    assert "last_success_utc" not in attrs
    assert attrs == {}

    monkeypatch.setattr(
        binary_sensor,
        "_type_device_info",
        lambda _coord, _type_key: None,
    )
    info = sensor.device_info
    assert info["identifiers"] == {(DOMAIN, f"type:{coord.site_id}:cloud")}
    assert info["manufacturer"] == "Enphase"
    assert info["model"] == "Cloud Service"
    assert info["name"] == "Enphase Cloud"


def test_site_active_system_events_binary_sensor_metadata(
    coordinator_factory, monkeypatch
) -> None:
    """Expose only the sanitized cached event summary on the cloud device."""

    coord = coordinator_factory(serials=[], data={})
    runtime = coord.system_events_runtime
    sensor = SiteActiveSystemEventsBinarySensor(coord)

    assert sensor.translation_key == "active_system_events"
    assert sensor.device_class == BinarySensorDeviceClass.PROBLEM
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor.available is False
    assert sensor.is_on is False

    runtime._last_success_utc = datetime.now(timezone.utc)  # noqa: SLF001
    runtime._events = (  # noqa: SLF001
        SimpleNamespace(
            high_impact=True,
            severity="critical",
            device_type="IQ Gateway",
            event_type="Gateway fault",
            state="open",
            event_date="2026-07-11T01:02:03Z",
            updated_at="2026-07-11T01:03:03Z",
        ),
    )
    attrs = sensor.extra_state_attributes
    assert sensor.available is True
    assert sensor.is_on is True
    assert attrs["active_count"] == 1
    assert attrs["high_impact_count"] == 1
    assert attrs["standing_alarm_count"] == 0
    assert attrs["severity_counts"] == {}
    assert attrs["device_type_counts"] == {"IQ Gateway": 1}
    assert attrs["active_events"] == (
        {
            "type": "Gateway fault",
            "device_type": "IQ Gateway",
            "state": "open",
            "event_date": "2026-07-11T01:02:03Z",
            "updated_at": "2026-07-11T01:03:03Z",
        },
    )
    assert "active_events" in sensor._unrecorded_attributes  # noqa: SLF001
    assert "serial_number" not in attrs

    runtime._events = (  # noqa: SLF001
        SimpleNamespace(
            high_impact=False,
            severity="unknown",
            device_type="IQ Gateway",
            event_type="Routine state change",
            state="open",
            event_date=None,
            updated_at=None,
        ),
    )
    assert sensor.is_on is False
    runtime._standing_alarms = (  # noqa: SLF001
        SimpleNamespace(
            severity="4",
            device_type="Gateway",
            first_set="2026-07-11T02:03:04Z",
        ),
    )
    assert sensor.is_on is True
    standing_attrs = sensor.extra_state_attributes
    assert standing_attrs["active_count"] == 1
    assert standing_attrs["device_type_counts"] == {"Gateway": 1}
    assert standing_attrs["active_events"] == (
        {
            "type": "Standing Alarm",
            "device_type": "Gateway",
            "state": "active",
            "event_date": "2026-07-11T02:03:04Z",
            "updated_at": None,
        },
    )

    monkeypatch.setattr(
        binary_sensor,
        "_type_device_info",
        lambda _coord, _type_key: None,
    )
    info = sensor.device_info
    assert info["identifiers"] == {(DOMAIN, f"type:{coord.site_id}:cloud")}

    monkeypatch.setattr(
        binary_sensor,
        "_type_device_info",
        lambda _coord, _type_key: {"name": "Custom Cloud"},
    )
    assert sensor.device_info == {"name": "Custom Cloud"}


def test_site_cloud_reachable_binary_sensor_metadata_includes_optional_failure_fields(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    sensor = SiteCloudReachableBinarySensor(coord)
    coord.last_success_utc = datetime.now(timezone.utc)
    coord.last_update_success = True
    coord.last_failure_endpoint = "/ivp/meters"
    coord.payload_failure_kind = "schema"
    coord.payload_using_stale = True

    attrs = sensor.extra_state_attributes
    assert attrs == {}


def test_binary_sensor_helper_type_available_uses_inventory_view() -> None:
    coord = SimpleNamespace(
        inventory_view=SimpleNamespace(
            has_type_for_entities=lambda type_key: type_key == "heatpump"
        ),
    )

    assert binary_sensor._type_available(coord, "heatpump") is True
    assert binary_sensor._type_available(coord, "envoy") is False


def test_heatpump_sg_ready_active_binary_sensor_metadata(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "name": "Heat Pump",
                        "model": "Europa Mini WP",
                        "serial_number": "HP-1",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    coord._heatpump_runtime_state = {  # noqa: SLF001
        "device_uid": "HP-1",
        "heatpump_status": "RUNNING",
        "sg_ready_mode_raw": "MODE_3",
        "sg_ready_mode_label": "Recommended",
        "sg_ready_active": True,
        "sg_ready_contact_state": "closed",
        "vpp_sgready_mode_override": "NONE",
        "last_report_at": "2026-03-03T07:30:00Z",
        "source": "hems_heatpump_state:HP-1",
    }
    coord._heatpump_runtime_state_last_error = None  # noqa: SLF001
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    sensor = HeatPumpSgReadyActiveBinarySensor(coord)
    assert sensor.translation_key == "heat_pump_sg_ready_active"
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor.available is True
    assert sensor.is_on is True

    assert sensor.extra_state_attributes == {}

    info = sensor.device_info
    assert info["name"] == "Heat Pump"
    assert info["model"] == "Europa Mini WP"
    assert info["serial_number"] == "HP-1"

    coord._heatpump_runtime_state = {  # noqa: SLF001
        **coord._heatpump_runtime_state,
        "sg_ready_mode_raw": "MODE_2",
        "sg_ready_mode_label": "Normal",
        "sg_ready_active": False,
        "sg_ready_contact_state": "open",
    }
    assert sensor.is_on is False

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "name": "Heat Pump"}],
            }
        },
        ["heatpump"],
    )
    assert sensor.available is True


def test_heatpump_sg_ready_active_binary_sensor_uses_dedicated_hems_inventory(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord._hems_devices_payload = {  # noqa: SLF001
        "data": {
            "hems-devices": {
                "heat-pump": [
                    {
                        "device-type": "SG_READY_GATEWAY",
                        "device-uid": "HP-SG-1",
                        "name": "SG Ready Gateway",
                        "statusText": "Recommended",
                        "last-report": "2026-03-03T07:30:00Z",
                        "model": "Europa Mini WP",
                        "serial": "HP-1",
                    },
                    {
                        "device-type": "HEAT_PUMP",
                        "device-uid": "HP-CTRL-1",
                        "name": "Heat Pump",
                        "statusText": "Normal",
                    },
                ]
            }
        }
    }
    coord.inventory_runtime._merge_heatpump_type_bucket()  # noqa: SLF001
    coord._heatpump_runtime_state = {  # noqa: SLF001
        "device_uid": "HP-CTRL-1",
        "heatpump_status": "RUNNING",
        "sg_ready_mode_raw": "MODE_3",
        "sg_ready_mode_label": "Recommended",
        "sg_ready_active": True,
        "last_report_at": "2026-03-03T07:30:00Z",
    }
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    sensor = HeatPumpSgReadyActiveBinarySensor(coord)
    assert sensor.available is True
    assert sensor.is_on is True
    assert (
        sensor.unique_id == f"{DOMAIN}_site_{coord.site_id}_heat_pump_sg_ready_active"
    )
    assert sensor.extra_state_attributes == {}


def test_heatpump_sg_ready_active_binary_sensor_stays_on_for_mixed_member_statuses(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 3,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "name": "Heat Pump",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG-1",
                        "name": "SG Ready Gateway 1",
                        "statusText": "Recommended",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG-2",
                        "name": "SG Ready Gateway 2",
                        "statusText": "Normal",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    coord._heatpump_runtime_state = {  # noqa: SLF001
        "device_uid": "HP-1",
        "heatpump_status": "RUNNING",
        "sg_ready_mode_raw": "MODE_3",
        "sg_ready_mode_label": "Recommended",
        "sg_ready_active": True,
    }
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    sensor = HeatPumpSgReadyActiveBinarySensor(coord)
    assert sensor.available is True
    assert sensor.is_on is True
    assert sensor.extra_state_attributes == {}


def test_heatpump_sg_ready_active_binary_sensor_helper_edge_cases(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "name": "Heat Pump",
                    }
                ],
            }
        },
        ["heatpump"],
    )
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )
    sensor = HeatPumpSgReadyActiveBinarySensor(coord)

    coord._heatpump_runtime_state = {  # noqa: SLF001
        "device_uid": "HP-1",
        "sg_ready_mode_raw": "MODE_3",
        "sg_ready_mode_label": "Recommended",
        "sg_ready_active": True,
    }
    assert sensor.available is True
    assert sensor.is_on is True

    coord._heatpump_runtime_state = {  # noqa: SLF001
        "device_uid": "HP-1",
        "sg_ready_mode_raw": "MODE_2",
        "sg_ready_mode_label": "Normal",
        "sg_ready_active": False,
    }
    assert sensor.is_on is False

    monkeypatch.setattr(sensor, "_snapshot", lambda: {})
    assert sensor.available is False


def test_heatpump_sg_ready_active_binary_sensor_unavailable_without_type(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord.inventory_view.has_type_for_entities = lambda _type_key: False  # type: ignore[assignment]
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )

    sensor = HeatPumpSgReadyActiveBinarySensor(coord)
    assert sensor.available is False


def test_heatpump_sg_ready_active_binary_sensor_handles_runtime_uid_errors(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord._heatpump_runtime_state = {  # noqa: SLF001
        "device_uid": "HP-1",
        "sg_ready_mode_raw": "MODE_3",
        "sg_ready_mode_label": "Recommended",
        "sg_ready_active": True,
    }
    monkeypatch.setattr(
        coord,
        "_heatpump_runtime_device_uid",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    sensor = HeatPumpSgReadyActiveBinarySensor(coord)
    assert sensor.available is False


def test_heatpump_sg_ready_active_binary_sensor_requires_runtime_uid(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord._heatpump_runtime_state = {  # noqa: SLF001
        "device_uid": "HP-1",
        "sg_ready_mode_raw": "MODE_2",
        "sg_ready_mode_label": "Normal",
        "sg_ready_active": True,
    }
    monkeypatch.setattr(coord, "_heatpump_runtime_device_uid", lambda: "")

    sensor = HeatPumpSgReadyActiveBinarySensor(coord)
    assert sensor.available is False


def test_site_cloud_reachable_binary_sensor_fallback_paths(
    coordinator_factory, monkeypatch
) -> None:
    coord = coordinator_factory(serials=[], data={})
    monkeypatch.setattr(
        coord, "async_add_topology_listener", lambda callback: _stub_listener()
    )
    coord.inventory_view.has_type_for_entities = lambda _type_key: False  # type: ignore[assignment]
    coord.last_update_success = False

    sensor = SiteCloudReachableBinarySensor(coord)
    assert sensor.available is False

    coord.last_update_success = True
    coord.last_success_utc = datetime.now(timezone.utc)
    assert sensor.available is True

    coord.last_success_utc = None
    assert sensor.is_on is False

    info = sensor.device_info
    assert info["identifiers"] == {(DOMAIN, f"type:{coord.site_id}:cloud")}

    custom_info = {"name": "Custom Cloud"}
    coord.inventory_view.type_device_info = lambda _type_key: custom_info  # type: ignore[assignment]
    assert sensor.device_info is custom_info
