"""Additional coverage for Enphase Energy sensor helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.entity import EntityCategory

from custom_components.enphase_ev import sensor as sensor_mod
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.sensor_battery_helpers import (
    battery_last_reported_snapshot,
)

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL

pytest.importorskip("homeassistant")


def _mk_coord(sn: str, payload: dict[str, Any]) -> Any:
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    coord = EnphaseCoordinator.__new__(EnphaseCoordinator)
    coord.hass = SimpleNamespace()
    coord.hass.config = SimpleNamespace(units=SimpleNamespace(length_unit="mi"))
    coord.data = {sn: payload}
    coord.serials = {sn}
    coord.last_set_amps = {}
    coord.site_id = "123456"
    coord._serial_order = [sn]
    coord.iter_serials = lambda: list(coord.serials)

    def _default_listener(callback, context=None):
        return lambda: None

    coord.async_add_topology_listener = _default_listener  # type: ignore[assignment]
    coord.last_success_utc = None
    coord.last_failure_utc = None
    coord.last_failure_status = None
    coord.last_failure_description = None
    coord.last_failure_source = None
    coord.last_failure_response = None
    coord.backoff_ends_utc = None
    coord.latency_ms = None
    coord.last_update_success = True
    return coord


def _mark_battery_status_ready(coord: Any) -> None:
    coord._battery_status_payload = {}  # noqa: SLF001


def _mark_inverter_inventory_ready(coord: Any) -> None:
    coord._inverters_inventory_payload = {}  # noqa: SLF001


def test_ac_battery_status_fallback_serials_guard_paths() -> None:
    coord = SimpleNamespace(
        battery_has_acb=True,
        inventory_view=SimpleNamespace(has_type_for_entities=lambda _key: True),
        ac_battery_status_summary={"status_source": "battery_status"},
    )

    assert sensor_mod._ac_battery_status_fallback_serials_for_setup(coord) is None

    class BadSerial:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    coord.iter_ac_battery_serials = lambda: [BadSerial()]

    assert sensor_mod._ac_battery_status_fallback_serials_for_setup(coord) is None


@pytest.mark.asyncio
async def test_async_setup_entry_registers_entities(
    hass, config_entry, coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.data[RANDOM_SERIAL].update(
        {
            "connector_status": "AVAILABLE",
            "charge_mode": "IMMEDIATE",
            "plugged": True,
            "charging_level": 32,
        }
    )
    coord.grid_profile_runtime = SimpleNamespace(
        installer_access_confirmed=True,
        installer_access_ever_confirmed=True,
        support_state="installer_access_confirmed",
        current_profile_display=MagicMock(return_value="Australia A Region (1.3.12)"),
        current_profile_attributes=MagicMock(
            return_value={"profile_id": "agf:current"}
        ),
    )
    callbacks: list[Any] = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_topology_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added = []

    def _async_add_entities(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _async_add_entities)
    assert any(ent.unique_id.endswith("_energy_today") for ent in added)
    assert any(ent.unique_id.endswith("_last_rpt") for ent in added)
    assert any(ent.unique_id.endswith("_electrical_phase") for ent in added)
    assert any(ent.unique_id.endswith("_charger_authentication") for ent in added)
    assert len([ent for ent in added if hasattr(ent, "_sn")]) == 11
    grid_profile = next(
        ent for ent in added if ent.unique_id.endswith("_current_grid_profile")
    )
    assert grid_profile.available
    assert grid_profile.native_value == "Australia A Region (1.3.12)"
    assert grid_profile.extra_state_attributes == {"profile_id": "agf:current"}

    sync_topology_cb = next(
        cb for cb in callbacks if cb.__name__ == "_async_sync_topology"
    )
    sync_topology_cb()
    assert len([ent for ent in added if hasattr(ent, "_sn")]) == 11

    new_sn = "NEWSN123"
    coord.data[new_sn] = dict(coord.data[RANDOM_SERIAL], sn=new_sn)
    coord.serials.add(new_sn)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_label": "EV Chargers",
                "count": 2,
                "devices": [
                    {"serial_number": RANDOM_SERIAL},
                    {"serial_number": new_sn},
                ],
            }
        },
        ["iqevse"],
    )
    sync_topology_cb()
    assert len({ent._sn for ent in added if hasattr(ent, "_sn")}) == 2


@pytest.mark.asyncio
async def test_large_site_unchanged_update_skips_registry_reconciliation(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    """A telemetry-only update must stay constant-cost for a large site."""

    from custom_components.enphase_ev.sensor import (
        EnphaseInverterLifetimeEnergySensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    inverter_serials = [f"INV-{index:04d}" for index in range(500)]
    coord._inverter_data = {  # noqa: SLF001
        serial: {
            "serial_number": serial,
            "lifetime_production_wh": index * 1000,
        }
        for index, serial in enumerate(inverter_serials)
    }
    coord._inverter_order = inverter_serials  # noqa: SLF001
    _mark_inverter_inventory_ready(coord)
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": len(inverter_serials),
                "devices": [{"serial_number": serial} for serial in inverter_serials],
            }
        },
        ["microinverter"],
    )
    topology_callbacks: list[Any] = []
    coordinator_callbacks: list[Any] = []
    coord.async_add_topology_listener = (  # type: ignore[assignment]
        lambda callback: topology_callbacks.append(callback) or (lambda: None)
    )
    coord.async_add_listener = (  # type: ignore[assignment]
        lambda callback, *args, **kwargs: coordinator_callbacks.append(callback)
        or (lambda: None)
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    class CountingEntities(dict[str, object]):
        values_calls = 0

        def values(self):  # type: ignore[override]
            self.values_calls += 1
            return super().values()

    entities = CountingEntities()
    fake_registry = SimpleNamespace(
        entities=entities,
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(return_value=None),
    )
    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: fake_registry)
    added: list[Any] = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda new, update_before_add=False: added.extend(new),
    )

    assert (
        len(
            [
                entity
                for entity in added
                if isinstance(entity, EnphaseInverterLifetimeEnergySensor)
            ]
        )
        == 500
    )
    capability_callback = next(
        callback
        for callback in coordinator_callbacks
        if callback.__name__ == "_async_sync_capabilities"
    )
    lookup_calls = fake_registry.async_get_entity_id.call_count
    values_calls = entities.values_calls
    added_count = len(added)

    capability_callback()

    assert fake_registry.async_get_entity_id.call_count == lookup_calls
    assert entities.values_calls == values_calls
    assert len(added) == added_count

    coord.tariff_billing = {"cycle": "monthly"}
    capability_callback()

    assert len(added) > added_count


def test_per_update_device_snapshots_are_reused(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseAcBatteryStorageChargeSensor,
        EnphaseBatteryStorageChargeSensor,
        EnphaseInverterLifetimeEnergySensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {  # noqa: SLF001
        "BAT-1": {"serial_number": "BAT-1", "current_charge_pct": 42}
    }
    coord._inverter_data = {  # noqa: SLF001
        "INV-1": {"serial_number": "INV-1", "lifetime_production_wh": 1000}
    }
    coord._ac_battery_data = {  # noqa: SLF001
        "AC-1": {"serial_number": "AC-1", "current_charge_pct": 37}
    }
    original_battery_storage = coord.battery_storage
    original_inverter_data = coord.inverter_data
    original_ac_battery_storage = coord.ac_battery_storage
    coord.battery_storage = MagicMock(side_effect=original_battery_storage)  # type: ignore[method-assign]
    coord.inverter_data = MagicMock(side_effect=original_inverter_data)  # type: ignore[method-assign]
    coord.ac_battery_storage = MagicMock(side_effect=original_ac_battery_storage)  # type: ignore[method-assign]
    battery = EnphaseBatteryStorageChargeSensor(coord, "BAT-1")
    inverter = EnphaseInverterLifetimeEnergySensor(coord, "INV-1")
    ac_battery = EnphaseAcBatteryStorageChargeSensor(coord, "AC-1")

    assert battery.available is True
    assert battery.native_value == 42
    assert battery.extra_state_attributes["serial_number"] == "BAT-1"
    assert inverter.available is True
    assert inverter.native_value == 1
    assert inverter.extra_state_attributes["sampled_at_utc"] is None
    assert ac_battery.native_value == 37
    assert ac_battery.extra_state_attributes["battery_id"] is None
    assert coord.battery_storage.call_count == 1
    assert coord.inverter_data.call_count == 1
    assert coord.ac_battery_storage.call_count == 1

    coord.data = dict(coord.data)
    assert battery.native_value == 42
    assert inverter.native_value == 1
    assert ac_battery.native_value == 37
    assert coord.battery_storage.call_count == 2
    assert coord.inverter_data.call_count == 2
    assert coord.ac_battery_storage.call_count == 2

    coord._battery_storage_data = {}  # noqa: SLF001
    coord.data = dict(coord.data)
    assert battery.native_value is None
    assert battery.native_value is None
    assert coord.battery_storage.call_count == 3

    coord._inverter_data = {  # noqa: SLF001
        "INV-1": {"serial_number": "INV-1", "lifetime_production_wh": "invalid"}
    }
    coord.data = dict(coord.data)
    assert inverter.native_value == 1
    coord._inverter_data = {  # noqa: SLF001
        "INV-1": {"serial_number": "INV-1", "lifetime_production_wh": 500}
    }
    coord.data = dict(coord.data)
    assert inverter.native_value == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("support_state", ["unknown", "activation_unavailable"])
async def test_async_setup_preserves_grid_profile_registry_during_startup_probe(
    hass, config_entry, coordinator_factory, support_state
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.grid_profile_runtime = SimpleNamespace(
        installer_access_confirmed=False,
        installer_access_ever_confirmed=False,
        support_state=support_state,
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}_site_{coord.site_id}_current_grid_profile"
    entity_id = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id,
        suggested_object_id="grid_profile",
    ).entity_id
    ent_reg.async_update_entity(entity_id, name="Custom Grid Profile")
    added: list[Any] = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    preserved = ent_reg.async_get(entity_id)
    assert preserved is not None
    assert preserved.name == "Custom Grid Profile"
    assert not any(
        entity.unique_id.endswith("_current_grid_profile") for entity in added
    )


@pytest.mark.asyncio
async def test_async_setup_entry_restored_topology_adds_dynamic_entities(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {  # noqa: SLF001
        "BAT-1": {"serial_number": "BAT-1", "name": "Battery 1"}
    }
    coord._battery_storage_order = ["BAT-1"]  # noqa: SLF001
    _mark_battery_status_ready(coord)
    coord._inverter_data = {  # noqa: SLF001
        "INV-1": {"serial_number": "INV-1", "name": "Inverter 1"}
    }
    coord._inverter_order = ["INV-1"]  # noqa: SLF001
    _mark_inverter_inventory_ready(coord)
    coord._restored_site_energy_channels = {"heat_pump"}  # noqa: SLF001
    coord._restored_gateway_iq_energy_router_records = [  # noqa: SLF001
        {
            "device-uid": "ROUTER-1",
            "device-type": "IQ_ENERGY_ROUTER",
            "name": "IQ Energy Router_1",
        }
    ]
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1", "name": "Gateway"}],
            },
            "encharge": {
                "type_label": "Batteries",
                "count": 1,
                "devices": [{"serial_number": "BAT-1", "name": "Battery 1"}],
            },
            "microinverter": {
                "type_label": "Microinverters",
                "count": 1,
                "devices": [{"serial_number": "INV-1", "name": "Inverter 1"}],
            },
            "heatpump": {
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"serial_number": "HP-1", "name": "Heat Pump"}],
            },
        },
        ["envoy", "encharge", "microinverter", "heatpump"],
    )
    coord.energy.site_energy = {}
    coord.energy._site_energy_meta = {"bucket_lengths": {}}  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _async_add_entities(entities, update_before_add=False) -> None:
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _async_add_entities)

    class_names = {entity.__class__.__name__ for entity in added}
    assert "EnphaseBatteryStorageChargeSensor" in class_names
    assert "EnphaseMicroinverterConnectivityStatusSensor" in class_names
    assert "EnphaseGatewayIQEnergyRouterSensor" in class_names
    assert any(getattr(entity, "_flow_key", None) == "heat_pump" for entity in added)


@pytest.mark.asyncio
async def test_async_setup_entry_backfills_storm_guard_sensor_when_battery_appears(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_has_encharge = False  # noqa: SLF001
    callbacks: list[Any] = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_topology_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _async_add_entities(entities, update_before_add=False) -> None:
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _async_add_entities)

    assert not any(
        entity.__class__.__name__ == "EnphaseStormGuardStateSensor" for entity in added
    )

    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_storage_data = {  # noqa: SLF001
        "BAT-1": {"serial_number": "BAT-1", "name": "Battery 1"}
    }
    coord._battery_storage_order = ["BAT-1"]  # noqa: SLF001
    _mark_battery_status_ready(coord)

    sync_topology_cb = next(
        cb for cb in callbacks if cb.__name__ == "_async_sync_topology"
    )
    sync_topology_cb()

    storm_guard_entities = [
        entity
        for entity in added
        if entity.__class__.__name__ == "EnphaseStormGuardStateSensor"
    ]
    assert len(storm_guard_entities) == 1
    assert storm_guard_entities[0]._sn == RANDOM_SERIAL


@pytest.mark.asyncio
async def test_async_setup_entry_site_energy_known_error_falls_back_to_bucket_lengths(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.energy = SimpleNamespace(  # type: ignore[assignment]
        site_energy={},
        site_energy_meta={"bucket_lengths": {"water_heater": 1}},
    )

    def _raise_known(_flow_key: str) -> bool:
        raise RuntimeError("boom")

    coord.discovery_snapshot.site_energy_channel_known = _raise_known  # type: ignore[assignment]
    callbacks: list = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_topology_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    created: list[Any] = []

    class StubSiteEnergy(sensor_mod.EnphaseSiteEnergySensor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(sensor_mod, "EnphaseSiteEnergySensor", StubSiteEnergy)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)
    for cb in callbacks:
        cb()

    assert any(ent._flow_key == "water_heater" for ent in created)


@pytest.mark.asyncio
async def test_async_setup_entry_battery_registry_ignores_unknown_unique_suffix(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {}  # noqa: SLF001
    coord._battery_storage_order = []  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    fake_registry = SimpleNamespace(
        entities={
            "sensor.battery_unknown": SimpleNamespace(
                domain="sensor",
                entity_id="sensor.battery_unknown",
                platform="enphase_ev",
                config_entry_id=config_entry.entry_id,
                unique_id=(
                    f"enphase_ev_site_{coord.site_id}_battery_BAT-123_unknown_suffix"
                ),
            )
        },
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_remove.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_removed_gateway_connected_devices_entity(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    unique_ids = {
        f"enphase_ev_site_{coord.site_id}_gateway_connected_devices": "sensor.gateway_connected_devices",
        f"enphase_ev_site_{coord.site_id}_type_microinverter_inventory": "sensor.microinverter_inventory",
    }
    fake_registry = SimpleNamespace(
        entities={},
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(
            side_effect=lambda domain, platform, candidate_unique_id: (
                unique_ids.get(candidate_unique_id)
                if domain == "sensor" and platform == "enphase_ev"
                else None
            ),
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert fake_registry.async_remove.call_count == 2
    fake_registry.async_remove.assert_any_call("sensor.gateway_connected_devices")
    fake_registry.async_remove.assert_any_call("sensor.microinverter_inventory")


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_historical_charger_sensor_entities(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.async_add_topology_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    stale_suffixes = (
        "_connector_reason",
        "_session_miles",
        "_plg_in_at",
        "_plg_out_at",
        "_schedule_type",
        "_schedule_start",
        "_schedule_end",
        "_session_kwh",
        "_charging_level",
        "_session_duration",
        "_phase_mode",
        "_max_current",
        "_min_amp",
        "_max_amp",
        "_connection",
        "_reporting_interval",
        "_ip_address",
    )

    entities: dict[str, Any] = {}
    expected_removed_ids: list[str] = []
    for index, suffix in enumerate(stale_suffixes):
        entity_id = f"sensor.historical_{index}"
        entities[entity_id] = SimpleNamespace(
            entity_id=entity_id,
            domain="sensor",
            platform=DOMAIN,
            unique_id=f"{DOMAIN}_{RANDOM_SERIAL}{suffix}",
            config_entry_id=config_entry.entry_id,
        )
        expected_removed_ids.append(entity_id)

    entities["sensor.keep_energy_today"] = SimpleNamespace(
        entity_id="sensor.keep_energy_today",
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_energy_today",
        config_entry_id=config_entry.entry_id,
    )
    entities["sensor.keep_power"] = SimpleNamespace(
        entity_id="sensor.keep_power",
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_power",
        config_entry_id=config_entry.entry_id,
    )
    entities["sensor.keep_status"] = SimpleNamespace(
        entity_id="sensor.keep_status",
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_status",
        config_entry_id=config_entry.entry_id,
    )
    entities["sensor.keep_charging_amps"] = SimpleNamespace(
        entity_id="sensor.keep_charging_amps",
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_charging_amps",
        config_entry_id=config_entry.entry_id,
    )
    entities["sensor.keep_last_reported"] = SimpleNamespace(
        entity_id="sensor.keep_last_reported",
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_last_rpt",
        config_entry_id=config_entry.entry_id,
    )
    entities["sensor.keep_electrical_phase"] = SimpleNamespace(
        entity_id="sensor.keep_electrical_phase",
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_electrical_phase",
        config_entry_id=config_entry.entry_id,
    )
    entities["switch.ignore_domain"] = SimpleNamespace(
        entity_id="switch.ignore_domain",
        domain="switch",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_session_duration",
        config_entry_id=config_entry.entry_id,
    )
    entities["sensor.ignore_platform"] = SimpleNamespace(
        entity_id="sensor.ignore_platform",
        domain="sensor",
        platform="other_domain",
        unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_phase_mode",
        config_entry_id=config_entry.entry_id,
    )
    entities["sensor.ignore_config_entry"] = SimpleNamespace(
        entity_id="sensor.ignore_config_entry",
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{RANDOM_SERIAL}_max_current",
        config_entry_id="other-entry-id",
    )

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = dict(entities)

        def async_remove(self, entity_id: str) -> None:
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

        def async_get_entity_id(
            self, domain: str, platform: str, unique_id: str
        ) -> str | None:
            return None

    fake_registry = FakeRegistry()
    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: fake_registry)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert sorted(removed_ids) == sorted(expected_removed_ids)
    assert "sensor.keep_energy_today" in fake_registry.entities
    assert "sensor.keep_power" in fake_registry.entities
    assert "sensor.keep_status" in fake_registry.entities
    assert "sensor.keep_charging_amps" in fake_registry.entities
    assert "sensor.keep_last_reported" in fake_registry.entities
    assert "sensor.keep_electrical_phase" in fake_registry.entities
    assert "switch.ignore_domain" in fake_registry.entities
    assert "sensor.ignore_platform" in fake_registry.entities
    assert "sensor.ignore_config_entry" in fake_registry.entities


@pytest.mark.asyncio
async def test_async_setup_entry_skips_battery_entities_without_battery(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryModeSensor,
        EnphaseStormAlertSensor,
        EnphaseStormGuardStateSensor,
        EnphaseSystemProfileStatusSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_has_encharge = False  # noqa: SLF001
    coord.data[RANDOM_SERIAL].update(
        {
            "connector_status": "AVAILABLE",
            "charge_mode": "IMMEDIATE",
            "plugged": True,
            "charging_level": 32,
        }
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _async_add_entities(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _async_add_entities)

    assert not any(isinstance(ent, EnphaseStormAlertSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseBatteryModeSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseSystemProfileStatusSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseStormGuardStateSensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_battery_storage_sensors(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryLastReportedSensor,
        EnphaseBatteryOverallChargeSensor,
        EnphaseBatteryOverallStatusSensor,
        EnphaseBatteryStorageCycleCountSensor,
        EnphaseBatteryStorageChargeSensor,
        EnphaseBatteryStorageHealthSensor,
        EnphaseBatteryStorageLastReportedSensor,
        EnphaseBatteryStorageStatusSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {  # noqa: SLF001
        "BAT-1": {
            "identity": "BAT-1",
            "name": "IQ Battery 5P",
            "serial_number": "BAT-1",
            "current_charge_pct": 48.0,
            "status": "normal",
        },
        "BAT-2": {
            "identity": "BAT-2",
            "name": "IQ Battery 5P",
            "serial_number": "BAT-2",
            "current_charge_pct": 47.0,
            "status": "normal",
        },
    }
    coord._battery_storage_order = ["BAT-1", "BAT-2"]  # noqa: SLF001
    _mark_battery_status_ready(coord)
    coord._battery_aggregate_charge_pct = 47.5  # noqa: SLF001
    coord._battery_aggregate_status = "normal"  # noqa: SLF001
    coord._battery_aggregate_status_details = {  # noqa: SLF001
        "included_count": 2,
        "excluded_count": 0,
        "per_battery_status": {"BAT-1": "normal", "BAT-2": "normal"},
        "battery_order": ["BAT-1", "BAT-2"],
    }
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    battery_entities = [
        ent for ent in added if isinstance(ent, EnphaseBatteryStorageChargeSensor)
    ]
    assert len(battery_entities) == 2
    assert (
        len(
            [ent for ent in added if isinstance(ent, EnphaseBatteryStorageStatusSensor)]
        )
        == 2
    )
    assert (
        len(
            [ent for ent in added if isinstance(ent, EnphaseBatteryStorageHealthSensor)]
        )
        == 2
    )
    assert (
        len(
            [
                ent
                for ent in added
                if isinstance(ent, EnphaseBatteryStorageCycleCountSensor)
            ]
        )
        == 2
    )
    assert (
        len(
            [
                ent
                for ent in added
                if isinstance(ent, EnphaseBatteryStorageLastReportedSensor)
            ]
        )
        == 0
    )
    assert any(isinstance(ent, EnphaseBatteryOverallChargeSensor) for ent in added)
    assert any(isinstance(ent, EnphaseBatteryOverallStatusSensor) for ent in added)
    assert any(isinstance(ent, EnphaseBatteryLastReportedSensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_removes_battery_entity_on_serial_drop(
    hass, config_entry, coordinator_factory
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {  # noqa: SLF001
        "BAT-REMOVE": {
            "identity": "BAT-REMOVE",
            "serial_number": "BAT-REMOVE",
            "current_charge_pct": 55,
        }
    }
    coord._battery_storage_order = ["BAT-REMOVE"]  # noqa: SLF001
    _mark_battery_status_ready(coord)
    callbacks: list[Any] = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_topology_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)
    ent_reg = er.async_get(hass)
    unique_id_charge = f"{DOMAIN}_site_{coord.site_id}_battery_BAT-REMOVE_charge_level"
    unique_id_status = f"{DOMAIN}_site_{coord.site_id}_battery_BAT-REMOVE_status"
    entity_id = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id_charge,
        suggested_object_id="battery_remove_charge",
    ).entity_id
    status_entity_id = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id_status,
        suggested_object_id="battery_remove_status",
    ).entity_id
    assert ent_reg.async_get(entity_id) is not None
    assert ent_reg.async_get(status_entity_id) is not None

    coord._battery_storage_data = {}  # noqa: SLF001
    coord._battery_storage_order = []  # noqa: SLF001
    _mark_battery_status_ready(coord)
    for callback in callbacks:
        callback()

    assert ent_reg.async_get(entity_id) is None
    assert ent_reg.async_get(status_entity_id) is None


@pytest.mark.asyncio
async def test_async_setup_entry_removes_stale_battery_entity_after_restart(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {}  # noqa: SLF001
    coord._battery_storage_order = []  # noqa: SLF001
    _mark_battery_status_ready(coord)
    coord.async_add_topology_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_BAT-OLD_status"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.bat_old_charge_level": SimpleNamespace(
                    entity_id="sensor.bat_old_status",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=unique_id,
                    config_entry_id=config_entry.entry_id,
                )
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == ["sensor.bat_old_status"]


@pytest.mark.asyncio
async def test_async_setup_entry_waits_for_authoritative_serial_family_payloads_before_prune(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_status_payload = None  # noqa: SLF001
    coord._ac_battery_devices_payload = None  # noqa: SLF001
    coord._inverters_inventory_payload = None  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord.include_inverters = True
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "encharge": {"type_label": "Batteries", "count": 1, "devices": []},
            "ac_battery": {"type_label": "AC Battery", "count": 1, "devices": []},
            "microinverter": {
                "type_label": "Microinverters",
                "count": 1,
                "devices": [],
            },
        },
        ["encharge", "ac_battery", "microinverter"],
    )
    coord.async_add_topology_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.bat_unknown": SimpleNamespace(
                    entity_id="sensor.bat_unknown",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=f"{DOMAIN}_site_{coord.site_id}_battery_BAT-OLD_status",
                    config_entry_id=config_entry.entry_id,
                ),
                "sensor.ac_bat_unknown": SimpleNamespace(
                    entity_id="sensor.ac_bat_unknown",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=f"{DOMAIN}_site_{coord.site_id}_ac_battery_ACBAT-OLD_status",
                    config_entry_id=config_entry.entry_id,
                ),
                "sensor.inv_unknown": SimpleNamespace(
                    entity_id="sensor.inv_unknown",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=f"{DOMAIN}_inverter_INV-OLD_lifetime_energy",
                    config_entry_id=config_entry.entry_id,
                ),
            }

        def async_get_entity_id(self, *_args):
            return None

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == []


@pytest.mark.asyncio
async def test_async_setup_entry_adds_ac_battery_status_fallback_without_prune(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._battery_has_acb = True  # noqa: SLF001
    coord._ac_battery_devices_payload = None  # noqa: SLF001
    coord._ac_battery_data = {  # noqa: SLF001
        "BAT-AC-1": {
            "serial_number": "BAT-AC-1",
            "current_charge_pct": 55.0,
            "status_normalized": "normal",
        }
    }
    coord._ac_battery_order = ["BAT-AC-1"]  # noqa: SLF001
    coord._ac_battery_aggregate_status_details = {  # noqa: SLF001
        "status_source": "battery_status"
    }
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {"ac_battery": {"type_label": "AC Battery", "count": 1, "devices": []}},
        ["ac_battery"],
    )
    coord.async_add_topology_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.ac_bat_old": SimpleNamespace(
                    entity_id="sensor.ac_bat_old",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=f"{DOMAIN}_site_{coord.site_id}_ac_battery_ACBAT-OLD_status",
                    config_entry_id=config_entry.entry_id,
                )
            }

        def async_get_entity_id(self, *_args):
            return None

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    per_ac_prefix = f"{DOMAIN}_site_{coord.site_id}_ac_battery_BAT-AC-1"
    per_ac_entities = [
        entity
        for entity in added
        if getattr(entity, "unique_id", "").startswith(per_ac_prefix)
    ]
    assert len(per_ac_entities) == 6
    assert removed_ids == []


@pytest.mark.asyncio
async def test_async_setup_entry_serial_family_sync_returns_when_source_becomes_unknown(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    responses = {
        "battery": [set(), None],
        "ac_battery": [set(), None],
        "inverter": [set(), None],
    }

    def _next_response(key: str):
        values = responses[key]
        return values.pop(0) if values else None

    monkeypatch.setattr(
        sensor_mod,
        "active_battery_serials_for_cleanup",
        lambda _coord: _next_response("battery"),
    )
    monkeypatch.setattr(
        sensor_mod,
        "active_ac_battery_serials_for_cleanup",
        lambda _coord: _next_response("ac_battery"),
    )
    monkeypatch.setattr(
        sensor_mod,
        "active_inverter_serials_for_cleanup",
        lambda _coord: _next_response("inverter"),
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert responses == {"battery": [], "ac_battery": [], "inverter": []}


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_battery_site_summary_entities_on_registry_prune(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {}  # noqa: SLF001
    coord._battery_storage_order = []  # noqa: SLF001
    coord.async_add_topology_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    overall_unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_overall_status"
    battery_last_reported_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_battery_last_reported"
    )

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.battery_overall_status": SimpleNamespace(
                    entity_id="sensor.battery_overall_status",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=overall_unique_id,
                    config_entry_id=config_entry.entry_id,
                ),
                "sensor.battery_last_reported": SimpleNamespace(
                    entity_id="sensor.battery_last_reported",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=battery_last_reported_unique_id,
                    config_entry_id=config_entry.entry_id,
                ),
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == []


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_removed_battery_site_sensor(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {}  # noqa: SLF001
    coord._battery_storage_order = []  # noqa: SLF001
    coord.async_add_topology_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    removed_unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_inactive_microinverters"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.battery_active_microinverters": SimpleNamespace(
                    entity_id="sensor.battery_active_microinverters",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=removed_unique_id,
                    config_entry_id=config_entry.entry_id,
                ),
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

        def async_get_entity_id(self, domain, platform, unique_id):
            if (
                domain == "sensor"
                and platform == DOMAIN
                and unique_id == removed_unique_id
            ):
                return "sensor.battery_active_microinverters"
            return None

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == ["sensor.battery_active_microinverters"]


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_current_battery_entity(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {  # noqa: SLF001
        "BAT-KEEP": {
            "identity": "BAT-KEEP",
            "serial_number": "BAT-KEEP",
            "current_charge_pct": 50,
        }
    }
    coord._battery_storage_order = ["BAT-KEEP"]  # noqa: SLF001
    _mark_battery_status_ready(coord)
    coord.async_add_topology_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_BAT-KEEP_charge_level"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.bat_keep_charge_level": SimpleNamespace(
                    entity_id="sensor.bat_keep_charge_level",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=unique_id,
                    config_entry_id=config_entry.entry_id,
                )
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == []


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_legacy_battery_last_reported_entities_for_active_serial(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {  # noqa: SLF001
        "BAT-KEEP": {
            "identity": "BAT-KEEP",
            "serial_number": "BAT-KEEP",
            "current_charge_pct": 50,
        }
    }
    coord._battery_storage_order = ["BAT-KEEP"]  # noqa: SLF001
    _mark_battery_status_ready(coord)
    coord.async_add_topology_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    legacy_unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_BAT-KEEP_last_reported"
    legacy_at_unique_id = (
        f"{DOMAIN}_site_{coord.site_id}_battery_BAT-KEEP_last_reported_at"
    )
    current_unique_id = f"{DOMAIN}_site_{coord.site_id}_battery_BAT-KEEP_status"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.bat_keep_last_reported": SimpleNamespace(
                    entity_id="sensor.bat_keep_last_reported",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=legacy_unique_id,
                    config_entry_id=config_entry.entry_id,
                ),
                "sensor.bat_keep_last_reported_at": SimpleNamespace(
                    entity_id="sensor.bat_keep_last_reported_at",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=legacy_at_unique_id,
                    config_entry_id=config_entry.entry_id,
                ),
                "sensor.bat_keep_status": SimpleNamespace(
                    entity_id="sensor.bat_keep_status",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=current_unique_id,
                    config_entry_id=config_entry.entry_id,
                ),
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert sorted(removed_ids) == [
        "sensor.bat_keep_last_reported",
        "sensor.bat_keep_last_reported_at",
    ]


@pytest.mark.asyncio
async def test_async_setup_entry_ignores_empty_battery_serial_unique_id(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {}  # noqa: SLF001
    coord._battery_storage_order = []  # noqa: SLF001
    coord.async_add_topology_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    unique_id = f"{DOMAIN}_site_{coord.site_id}_battery__status"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.empty_serial_status": SimpleNamespace(
                    entity_id="sensor.empty_serial_status",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=unique_id,
                    config_entry_id=config_entry.entry_id,
                )
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == []


@pytest.mark.asyncio
async def test_async_setup_entry_adds_inverter_lifetime_sensors(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.sensor import (
        EnphaseInverterLifetimeEnergySensor,
        EnphaseInverterTelemetrySensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {
            "serial_number": "INV-A",
            "name": "IQ7A",
            "inverter_id": "1001",
            "device_id": 42,
            "lifetime_production_wh": 1_500_000,
            "lifetime_query_start_date": "2022-08-10",
            "lifetime_query_end_date": "2026-02-09",
            "telemetry": {"power": 220.0},
        }
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    _mark_inverter_inventory_ready(coord)
    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 1,
            "devices": [{"serial_number": "INV-A"}],
            "model_summary": "IQ7A x1",
            "status_summary": "Normal 1 | Warning 0 | Error 0 | Not Reporting 0",
        }
    }
    coord._type_device_order = ["microinverter"]  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)
    inverter_entities = [
        ent for ent in added if isinstance(ent, EnphaseInverterLifetimeEnergySensor)
    ]
    assert len(inverter_entities) == 1
    assert any(isinstance(ent, EnphaseInverterTelemetrySensor) for ent in added)
    entity = inverter_entities[0]
    assert entity.native_value == pytest.approx(1500.0)
    attrs = entity.extra_state_attributes
    assert attrs == {
        "sampled_at_utc": None,
        "status": None,
        "status_text": None,
        "rssi": None,
    }
    assert set(attrs) <= entity._unrecorded_attributes  # noqa: SLF001

    # Entity reports unavailable once the inverter snapshot is removed.
    coord._inverter_data = {}  # noqa: SLF001
    assert entity.available is False


def test_inverter_telemetry_sensor(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import EnphaseInverterTelemetrySensor

    coord = coordinator_factory()
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {
            "telemetry": {
                "power": 234.5,
                "ac_voltage": 230.1,
                "ac_frequency": 49.99,
                "temperature": 42,
                "sampled_at": {"power": "2026-07-11T01:00:00Z"},
                "parameter_ids": {"power": "power"},
            },
            "fw1": "v1",
            "fw2": "v2",
            "rssi": {"sig_str": 72},
        }
    }
    entity = EnphaseInverterTelemetrySensor(coord, "INV-A")

    assert entity.available is True
    assert entity.native_value == 234.5
    assert entity.translation_key == "inverter_telemetry"
    assert entity.translation_placeholders == {"serial_number": "INV-A"}
    assert entity.entity_category is None
    assert entity.entity_registry_enabled_default is False
    assert entity.extra_state_attributes == {
        "power_w": 234.5,
        "ac_voltage_v": 230.1,
        "ac_frequency_hz": 49.99,
        "temperature_c": 42,
        "sampled_at": {"power": "2026-07-11T01:00:00Z"},
        "parameter_ids": {"power": "power"},
        "firmware_primary": "v1",
        "firmware_secondary": "v2",
        "signal_strength": {"sig_str": 72},
    }
    coord.inventory_view.type_device_info = lambda _type: {"name": "Shared"}
    assert entity.device_info == {"name": "Shared"}
    coord.inventory_view.type_device_info = lambda _type: None
    assert entity.device_info["name"] == "IQ Microinverters"
    coord._inverter_data["INV-A"]["telemetry"]["power"] = "bad"  # noqa: SLF001
    assert entity.native_value is None
    coord._inverter_data = {}  # noqa: SLF001
    assert entity.available is False


@pytest.mark.asyncio
async def test_async_setup_entry_removes_deleted_inverter_entity(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {"serial_number": "INV-A", "lifetime_production_wh": 100}
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    _mark_inverter_inventory_ready(coord)
    callbacks: list[Any] = []

    def _add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_topology_listener = _add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    unique_id = f"{DOMAIN}_inverter_INV-A_lifetime_energy"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.inv_a_lifetime_energy": SimpleNamespace(
                    entity_id="sensor.inv_a_lifetime_energy",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=unique_id,
                    config_entry_id=config_entry.entry_id,
                )
            }

        def async_get_entity_id(self, domain, platform, candidate_unique_id):
            if (
                domain == "sensor"
                and platform == DOMAIN
                and candidate_unique_id == unique_id
            ):
                return "sensor.inv_a_lifetime_energy"
            return None

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)
    sync_inverters = next(
        cb for cb in callbacks if cb.__name__ == "_async_sync_topology"
    )

    coord._inverter_data = {}  # noqa: SLF001
    coord._inverter_order = []  # noqa: SLF001
    _mark_inverter_inventory_ready(coord)
    sync_inverters()

    assert removed_ids == ["sensor.inv_a_lifetime_energy"]


@pytest.mark.asyncio
async def test_async_setup_entry_removes_stale_inverter_entity_after_restart(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._inverters_inventory_payload = {"inverters": []}  # noqa: SLF001
    coord._inverter_data = {}  # noqa: SLF001
    coord._inverter_order = []  # noqa: SLF001
    coord.async_add_topology_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []
    unique_id = f"{DOMAIN}_inverter_INV-Z_lifetime_energy"

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.inv_z_lifetime_energy": SimpleNamespace(
                    entity_id="sensor.inv_z_lifetime_energy",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=unique_id,
                    config_entry_id=config_entry.entry_id,
                )
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == ["sensor.inv_z_lifetime_energy"]


@pytest.mark.asyncio
async def test_async_setup_entry_registry_cleanup_filters_irrelevant_entries(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._inverters_inventory_payload = {"inverters": []}  # noqa: SLF001
    coord._inverter_data = {}  # noqa: SLF001
    coord._inverter_order = []  # noqa: SLF001
    coord.async_add_topology_listener = lambda _cb: (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    removed_ids: list[str] = []

    class FakeRegistry:
        def __init__(self) -> None:
            self.entities = {
                "sensor.stale_should_remove": SimpleNamespace(
                    entity_id="sensor.stale_should_remove",
                    domain=None,
                    platform=DOMAIN,
                    unique_id=f"{DOMAIN}_inverter_INV-OLD_lifetime_energy",
                    config_entry_id=config_entry.entry_id,
                ),
                "switch.ignore_domain": SimpleNamespace(
                    entity_id="switch.ignore_domain",
                    domain="switch",
                    platform=DOMAIN,
                    unique_id=f"{DOMAIN}_inverter_INV-DOMAIN_lifetime_energy",
                    config_entry_id=config_entry.entry_id,
                ),
                "sensor.ignore_platform": SimpleNamespace(
                    entity_id="sensor.ignore_platform",
                    domain="sensor",
                    platform="other_domain",
                    unique_id=f"{DOMAIN}_inverter_INV-PLATFORM_lifetime_energy",
                    config_entry_id=config_entry.entry_id,
                ),
                "sensor.ignore_config_entry": SimpleNamespace(
                    entity_id="sensor.ignore_config_entry",
                    domain="sensor",
                    platform=DOMAIN,
                    unique_id=f"{DOMAIN}_inverter_INV-CONFIG_lifetime_energy",
                    config_entry_id="different-entry-id",
                ),
            }

        def async_remove(self, entity_id):
            removed_ids.append(entity_id)
            self.entities.pop(entity_id, None)

    monkeypatch.setattr(sensor_mod.er, "async_get", lambda _hass: FakeRegistry())

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert removed_ids == ["sensor.stale_should_remove"]


def test_inverter_lifetime_sensor_clamps_regressions(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {"serial_number": "INV-A", "lifetime_production_wh": 2_000_000}
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")

    assert entity.native_value == pytest.approx(2000.0)
    coord._inverter_data["INV-A"]["lifetime_production_wh"] = 1_500_000  # noqa: SLF001
    assert entity.native_value == pytest.approx(2000.0)
    coord._inverter_data["INV-A"]["lifetime_production_wh"] = None  # noqa: SLF001
    assert entity.native_value == pytest.approx(2000.0)
    coord._inverter_data = {}  # noqa: SLF001
    assert entity.native_value == pytest.approx(2000.0)


def test_inverter_lifetime_sensor_handles_non_numeric_payload(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {  # noqa: SLF001
        "INV-A": {"serial_number": "INV-A", "lifetime_production_wh": 2_000_000}
    }
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    assert entity.native_value == pytest.approx(2000.0)

    coord._inverter_data["INV-A"]["lifetime_production_wh"] = "bad"  # noqa: SLF001
    assert entity.native_value == pytest.approx(2000.0)


@pytest.mark.asyncio
async def test_inverter_lifetime_sensor_restores_last_value(
    monkeypatch, coordinator_factory
):
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(native_value="3.25")
    )
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", AsyncMock())

    await entity.async_added_to_hass()
    assert entity.native_value == pytest.approx(3.25)


@pytest.mark.asyncio
async def test_inverter_lifetime_sensor_restore_handles_empty_and_invalid_data(
    monkeypatch, coordinator_factory
) -> None:
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", AsyncMock())

    entity_none = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity_none.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    await entity_none.async_added_to_hass()
    assert entity_none.native_value is None

    entity_bad = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity_bad.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(native_value=object())
    )
    await entity_bad.async_added_to_hass()
    assert entity_bad.native_value is None


@pytest.mark.asyncio
async def test_inverter_lifetime_sensor_restore_migrates_legacy_units(
    monkeypatch, coordinator_factory
) -> None:
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    class BadUnit:
        def __str__(self) -> str:
            raise ValueError("bad")

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", AsyncMock())

    entity_mwh = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity_mwh.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            native_value="1.5",
            native_unit_of_measurement="MWh",
        )
    )
    await entity_mwh.async_added_to_hass()
    assert entity_mwh.native_value == pytest.approx(1500.0)

    entity_wh = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity_wh.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            native_value="1500",
            native_unit_of_measurement="Wh",
        )
    )
    await entity_wh.async_added_to_hass()
    assert entity_wh.native_value == pytest.approx(1.5)

    entity_bad_unit = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity_bad_unit.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            native_value="2.0",
            native_unit_of_measurement=BadUnit(),
        )
    )
    await entity_bad_unit.async_added_to_hass()
    assert entity_bad_unit.native_value == pytest.approx(2.0)

    entity_inf = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity_inf.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            native_value=float("inf"),
            native_unit_of_measurement="kWh",
        )
    )
    await entity_inf.async_added_to_hass()
    assert entity_inf.native_value is None


@pytest.mark.asyncio
async def test_inverter_lifetime_sensor_restore_forces_kwh_unit(
    monkeypatch, coordinator_factory
) -> None:
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", AsyncMock())

    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    entity._attr_native_unit_of_measurement = UnitOfEnergy.MEGA_WATT_HOUR
    entity.async_get_last_sensor_data = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            native_value="1.5",
            native_unit_of_measurement="MWh",
        )
    )

    await entity.async_added_to_hass()

    assert entity.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    assert entity.native_value == pytest.approx(1500.0)


def test_inverter_lifetime_sensor_device_info_fallback(coordinator_factory) -> None:
    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.site_id = "123456"
    coord.inventory_view.type_device_info = lambda _key: None  # type: ignore[assignment]
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")

    info = entity.device_info
    assert info is not None
    assert (DOMAIN, "type:123456:microinverter") in info["identifiers"]


def test_inverter_lifetime_sensor_device_info_prefers_coordinator_info(
    coordinator_factory,
) -> None:
    from homeassistant.helpers.entity import DeviceInfo

    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    expected = DeviceInfo(
        identifiers={(DOMAIN, f"type:{coord.site_id}:microinverter")},
        manufacturer="Enphase",
        name="Microinverters",
    )
    coord.inventory_view.type_device_info = lambda _key: expected  # type: ignore[assignment]
    coord._inverter_data = {"INV-A": {"serial_number": "INV-A"}}  # noqa: SLF001
    coord._inverter_order = ["INV-A"]  # noqa: SLF001
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")

    assert entity.device_info is expected


def test_type_inventory_sensor_summary_attributes(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import EnphaseTypeInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._type_device_buckets = {  # noqa: SLF001
        "microinverter": {
            "type_key": "microinverter",
            "type_label": "Microinverters",
            "count": 2,
            "devices": [{"serial_number": "INV-A"}, {"serial_number": "INV-B"}],
            "status_counts": {
                "total": 2,
                "normal": 2,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
            },
            "status_summary": "Normal 2 | Warning 0 | Error 0 | Not Reporting 0",
            "model_counts": {"IQ7A": 2},
            "model_summary": "IQ7A x2",
            "firmware_counts": {"520-00082-r01-v04.30.32": 2},
            "firmware_summary": "520-00082-r01-v04.30.32 x2",
            "array_counts": {"North": 1, "West": 1},
            "array_summary": "North x1, West x1",
            "panel_info": {"pv_module_manufacturer": "Acme"},
            "status_type_counts": {"IQ7A": 2},
            "connectivity_state": "online",
            "reporting_count": 2,
            "latest_reported_utc": "2026-02-15T08:00:00+00:00",
            "latest_reported_device": {"serial_number": "INV-B"},
            "production_start_date": "2022-08-10",
            "production_end_date": "2026-02-15",
        }
    }
    coord._type_device_order = ["microinverter"]  # noqa: SLF001
    entity = EnphaseTypeInventorySensor(coord, "microinverter")

    assert entity.entity_registry_enabled_default is False
    attrs = entity.extra_state_attributes
    assert attrs["status_counts"]["normal"] == 2
    assert attrs["status_summary"].startswith("Normal 2")
    assert attrs["model_counts"]["IQ7A"] == 2
    assert attrs["model_summary"] == "IQ7A x2"
    assert attrs["firmware_summary"] == "520-00082-r01-v04.30.32 x2"
    assert attrs["array_summary"] == "North x1, West x1"
    assert attrs["panel_info"]["pv_module_manufacturer"] == "Acme"
    assert attrs["status_type_counts"]["IQ7A"] == 2
    assert attrs["connectivity_state"] == "online"
    assert attrs["reporting_count"] == 2
    assert attrs["latest_reported_device"]["serial_number"] == "INV-B"
    assert attrs["production_start_date"] == "2022-08-10"
    assert attrs["production_end_date"] == "2026-02-15"


def test_type_inventory_sensor_uses_iqevse_fallback_count(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import EnphaseTypeInventorySensor

    coord = coordinator_factory(serials=["EV1"])
    coord._selected_type_keys = {"iqevse"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {}  # noqa: SLF001
    coord.serials = {"EV1"}
    coord.data = {"EV1": {"sn": "EV1", "name": "Garage EV"}}
    coord.iter_serials = lambda: ["EV1"]

    entity = EnphaseTypeInventorySensor(coord, "iqevse")

    assert entity.available is True
    assert entity.native_value == 1


def test_type_inventory_sensor_iqevse_fallback_handles_iter_errors(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseTypeInventorySensor

    coord = coordinator_factory(serials=["EV1", "EV2"])
    coord._selected_type_keys = {"iqevse"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {
        "iqevse": {"count": "bad", "devices": "bad"}
    }  # noqa: SLF001
    coord.serials = {"EV1", "", "EV2"}
    coord.data = {}

    def _boom():
        raise RuntimeError("iter failed")

    coord.iter_serials = _boom

    entity = EnphaseTypeInventorySensor(coord, "iqevse")

    assert entity.native_value == 0


def test_type_inventory_sensor_iqevse_fallback_uses_serials_without_iterator(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseTypeInventorySensor

    coord = coordinator_factory(serials=["EV1", "EV2"])
    coord._selected_type_keys = {"iqevse"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {
        "iqevse": {"count": "bad", "devices": "bad"}
    }  # noqa: SLF001
    coord.serials = {"EV1", "", "EV2"}
    coord.data = {}
    coord.iter_serials = None

    entity = EnphaseTypeInventorySensor(coord, "iqevse")

    assert entity.native_value == 2


def test_type_inventory_sensor_iqevse_fallback_returns_zero_without_serials(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseTypeInventorySensor

    coord = coordinator_factory(serials=[])
    coord._selected_type_keys = {"iqevse"}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {
        "iqevse": {"count": "bad", "devices": "bad"}
    }  # noqa: SLF001
    coord.serials = object()
    coord.data = {}
    coord.iter_serials = None

    entity = EnphaseTypeInventorySensor(coord, "iqevse")

    assert entity.native_value == 0


def test_type_inventory_sensor_device_info_prefers_provider_and_fallback(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseTypeInventorySensor

    coord = coordinator_factory(serials=[])
    provided = {"identifiers": {(sensor_mod.DOMAIN, "provided")}, "name": "Provided"}
    coord.inventory_view.type_device_info = lambda _type_key: provided

    entity = EnphaseTypeInventorySensor(coord, "wind_turbine")
    assert entity.device_info is provided

    coord.inventory_view.type_device_info = lambda _type_key: None
    fallback = entity.device_info
    assert fallback["identifiers"] == {
        (sensor_mod.DOMAIN, f"type:{coord.site_id}:wind_turbine")
    }
    assert fallback["manufacturer"] == "Enphase"


def test_gateway_diagnostic_sensors_expose_inventory_summary(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConnectivityStatusSensor,
        EnphaseGatewayLastReportedSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 3,
                "devices": [
                    {
                        "name": "IQ Gateway",
                        "serial_number": "GW-1",
                        "connected": True,
                        "status": "normal",
                        "model": "IQ Gateway",
                        "envoy_sw_version": "8.2.0",
                        "ip_address": "192.0.2.10",
                        "last_report": "2026-02-15T18:00:00Z",
                    },
                    {
                        "name": "System Controller",
                        "serial_number": "GW-2",
                        "connected": False,
                        "statusText": "Not Reporting",
                        "channel_type": "enpower",
                        "sw_version": "8.2.0",
                    },
                    {
                        "name": "Meter",
                        "serial_number": "GW-3",
                        "status": "warning",
                        "last_report": 1_708_016_100_000,
                    },
                ],
            }
        },
        ["envoy"],
    )

    status_sensor = EnphaseGatewayConnectivityStatusSensor(coord)
    assert status_sensor.native_value == "Degraded"
    status_attrs = status_sensor.extra_state_attributes
    assert status_attrs["total_devices"] == 3
    assert status_attrs["connected_devices"] == 1
    assert status_attrs["disconnected_devices"] == 1
    assert status_attrs["unknown_connection_devices"] == 1
    assert status_attrs["status_counts"]["warning"] == 1
    assert status_attrs["ip_address"] == "192.0.2.10"

    last_reported_sensor = EnphaseGatewayLastReportedSensor(coord)
    assert last_reported_sensor.entity_registry_enabled_default is False
    assert last_reported_sensor.native_value is not None
    report_attrs = last_reported_sensor.extra_state_attributes
    assert report_attrs["latest_reported_device"]["serial_number"] == "GW-1"
    assert report_attrs["without_last_report_count"] == 1


def test_gateway_diagnostic_sensors_handle_missing_inventory(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConnectivityStatusSensor,
        EnphaseGatewayLastReportedSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._type_device_buckets = {"envoy": {"count": 0, "devices": []}}  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001

    assert EnphaseGatewayConnectivityStatusSensor(coord).native_value is None
    assert EnphaseGatewayLastReportedSensor(coord).native_value is None


def test_gateway_status_ip_helper_paths() -> None:
    assert (
        sensor_mod._gateway_member_ip_address(  # noqa: SLF001
            {"ip-address": " 192.0.2.20 "}
        )
        == "192.0.2.20"
    )
    assert (
        sensor_mod._gateway_ip_member_kind({"channel_type": "enpower"})  # noqa: SLF001
        == "controller"
    )
    assert (
        sensor_mod._gateway_ip_member_kind(  # noqa: SLF001
            {"channelType": "consumption_meter"}
        )
        == "consumption"
    )
    assert (
        sensor_mod._gateway_ip_member_kind({"name": "Main Controller"})  # noqa: SLF001
        == "controller"
    )
    assert (
        sensor_mod._gateway_ip_member_kind({"name": "Production Meter"})  # noqa: SLF001
        == "production"
    )
    assert (
        sensor_mod._gateway_ip_member_kind(  # noqa: SLF001
            {"name": "Consumption Meter"}
        )
        == "consumption"
    )
    assert (
        sensor_mod._gateway_summary_ip_address(  # noqa: SLF001
            [
                {"name": "Meter", "ip_address": "192.0.2.30"},
                {
                    "name": "Communications device",
                    "show_connection_details": True,
                    "ip_address": "192.0.2.31",
                },
            ],
            None,
        )
        == "192.0.2.31"
    )
    assert (
        sensor_mod._gateway_summary_ip_address(  # noqa: SLF001
            [{"name": "Production Meter", "ip_address": "192.0.2.32"}],
            None,
        )
        is None
    )
    assert (
        sensor_mod._gateway_summary_ip_address(  # noqa: SLF001
            [{"name": "Unknown Device", "ip_address": "192.0.2.32"}],
            None,
        )
        == "192.0.2.32"
    )
    assert (
        sensor_mod._gateway_summary_ip_address(  # noqa: SLF001
            [
                {
                    "name": "Production Meter",
                    "channel_type": "production_meter",
                    "show_connection_details": True,
                    "ip_address": "192.0.2.33",
                },
                {
                    "name": "System Controller",
                    "show_connection_details": True,
                    "ip_address": "192.0.2.34",
                },
                {
                    "name": "Communications device",
                    "show_connection_details": True,
                    "ip_address": "192.0.2.35",
                },
            ],
            None,
        )
        == "192.0.2.35"
    )


def test_microinverter_diagnostic_sensors_expose_inventory_summary(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseMicroinverterConnectivityStatusSensor,
        EnphaseMicroinverterLastReportedSensor,
        EnphaseMicroinverterReportingCountSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": 3,
                "devices": [
                    {
                        "name": "IQ7A",
                        "serial_number": "INV-A",
                        "statusText": "Normal",
                        "last_report": "2026-02-15T18:00:00Z",
                    },
                    {
                        "name": "IQ7A",
                        "serial_number": "INV-B",
                        "status": "NOT_REPORTING",
                        "last_report": 1_780_000_000_000,
                    },
                    {
                        "name": "IQ7A",
                        "serial_number": "INV-C",
                        "status": "normal",
                    },
                ],
                "status_counts": {
                    "total": 3,
                    "normal": 2,
                    "warning": 0,
                    "error": 0,
                    "not_reporting": 1,
                },
                "status_summary": "Normal 2 | Warning 0 | Error 0 | Not Reporting 1",
                "model_summary": "IQ7A x3",
                "firmware_summary": "520-00082-r01-v04.30.32 x3",
                "array_summary": "North x2, West x1",
                "panel_info": {"pv_module_manufacturer": "Acme"},
                "status_type_counts": {"IQ7A": 3},
                "production_start_date": "2022-08-10",
                "production_end_date": "2026-02-15",
            }
        },
        ["microinverter"],
    )

    status_sensor = EnphaseMicroinverterConnectivityStatusSensor(coord)
    assert status_sensor.native_value == "Degraded"
    status_attrs = status_sensor.extra_state_attributes
    assert status_attrs["total_inverters"] == 3
    assert status_attrs["reporting_inverters"] == 2
    assert status_attrs["not_reporting_inverters"] == 1
    assert status_attrs["unknown_inverters"] == 0
    assert set(status_attrs) == {
        "total_inverters",
        "reporting_inverters",
        "not_reporting_inverters",
        "unknown_inverters",
        "status_counts",
        "status_summary",
    }

    reporting_sensor = EnphaseMicroinverterReportingCountSensor(coord)
    assert reporting_sensor.native_value == 2
    reporting_attrs = reporting_sensor.extra_state_attributes
    assert reporting_attrs["type_key"] == "microinverter"
    assert reporting_attrs["type_label"] == "Microinverters"
    assert reporting_attrs["device_count"] == 3
    assert len(reporting_attrs["devices"]) == 3
    assert reporting_attrs["model_summary"] == "IQ7A x3"
    assert reporting_attrs["firmware_summary"] == "520-00082-r01-v04.30.32 x3"
    assert reporting_attrs["array_summary"] == "North x2, West x1"
    assert reporting_attrs["panel_info"]["pv_module_manufacturer"] == "Acme"
    assert reporting_attrs["status_type_counts"]["IQ7A"] == 3
    assert reporting_attrs["production_start_date"] == "2022-08-10"
    assert reporting_attrs["production_end_date"] == "2026-02-15"
    assert "connectivity_state" not in reporting_attrs
    assert "status_summary" not in reporting_attrs

    last_reported_sensor = EnphaseMicroinverterLastReportedSensor(coord)
    assert last_reported_sensor.entity_registry_enabled_default is False
    assert last_reported_sensor.available is True
    assert last_reported_sensor.native_value is not None
    report_attrs = last_reported_sensor.extra_state_attributes
    assert set(report_attrs) == {"latest_reported_device"}
    assert report_attrs["latest_reported_device"]["serial_number"] == "INV-B"


def test_last_reported_site_diagnostic_sensors_disabled_by_default(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseAcBatteryLastReportedSensor,
        EnphaseBatteryLastReportedSensor,
        EnphaseHeatPumpLastReportedSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])

    for entity in (
        EnphaseBatteryLastReportedSensor(coord),
        EnphaseAcBatteryLastReportedSensor(coord),
        EnphaseHeatPumpLastReportedSensor(coord),
    ):
        assert entity.entity_registry_enabled_default is False


def test_microinverter_diagnostic_sensors_handle_disabled_or_empty_inventory(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseMicroinverterConnectivityStatusSensor,
        EnphaseMicroinverterLastReportedSensor,
        EnphaseMicroinverterReportingCountSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._type_device_buckets = {
        "microinverter": {"count": 0, "devices": []}
    }  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001

    assert EnphaseMicroinverterConnectivityStatusSensor(coord).native_value is None
    assert EnphaseMicroinverterReportingCountSensor(coord).native_value is None
    assert EnphaseMicroinverterLastReportedSensor(coord).native_value is None

    coord.include_inverters = False
    assert EnphaseMicroinverterConnectivityStatusSensor(coord).available is False
    assert EnphaseMicroinverterReportingCountSensor(coord).available is False
    assert EnphaseMicroinverterLastReportedSensor(coord).available is False


def test_microinverter_snapshot_helper_handles_invalid_shapes(
    coordinator_factory,
) -> None:
    class BadInt:
        def __int__(self) -> int:
            raise ValueError("bad")

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_view.type_bucket = lambda _type_key: {  # type: ignore[assignment]
        "count": BadInt(),
        "devices": [{"serial_number": "INV-A", "last_report": "2026-02-15T10:00:00Z"}],
        "status_counts": {"total": BadInt(), "not_reporting": BadInt()},
    }

    snapshot = sensor_mod._microinverter_inventory_snapshot(coord)
    assert snapshot["total_inverters"] == 1
    assert snapshot["not_reporting_inverters"] == 0
    assert snapshot["unknown_inverters"] == 1
    assert snapshot["connectivity_state"] == "unknown"

    assert (
        sensor_mod._microinverter_connectivity_state(
            {"total_inverters": 3, "reporting_inverters": 3}
        )
        == "online"
    )
    assert (
        sensor_mod._microinverter_connectivity_state(
            {
                "total_inverters": 3,
                "reporting_inverters": 0,
                "not_reporting_inverters": 3,
            }
        )
        == "offline"
    )
    assert (
        sensor_mod._microinverter_connectivity_state(
            {"total_inverters": 3, "reporting_inverters": 0, "unknown_inverters": 3}
        )
        == "unknown"
    )
    assert (
        sensor_mod._microinverter_connectivity_state(
            {"total_inverters": 3, "reporting_inverters": 0, "unknown_inverters": 1}
        )
        == "degraded"
    )


def test_microinverter_snapshot_defaults_unknown_when_status_missing(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_view.type_bucket = lambda _type_key: {  # type: ignore[assignment]
        "count": 2,
        "devices": [{"serial_number": "INV-A"}, {"serial_number": "INV-B"}],
    }

    snapshot = sensor_mod._microinverter_inventory_snapshot(coord)
    assert snapshot["total_inverters"] == 2
    assert snapshot["reporting_inverters"] == 0
    assert snapshot["not_reporting_inverters"] == 0
    assert snapshot["unknown_inverters"] == 2
    assert snapshot["connectivity_state"] == "unknown"


def test_microinverter_snapshot_clamps_unknown_overflow(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_view.type_bucket = lambda _type_key: {  # type: ignore[assignment]
        "count": 1,
        "devices": [{"serial_number": "INV-A"}],
        "status_counts": {"total": 1, "not_reporting": 1, "unknown": 2},
    }

    snapshot = sensor_mod._microinverter_inventory_snapshot(coord)
    assert snapshot["total_inverters"] == 1
    assert snapshot["not_reporting_inverters"] == 1
    assert snapshot["unknown_inverters"] == 0
    assert snapshot["connectivity_state"] == "offline"


def test_microinverter_snapshot_fallback_covers_summary_error_and_zero_status_paths() -> (
    None
):
    coord = SimpleNamespace(
        microinverter_inventory_summary=lambda: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
        type_bucket=lambda _type_key: {
            "count": 2,
            "devices": [
                {
                    "serial_number": "INV-A",
                    "name": "Inverter A",
                    "status": "normal",
                    "last_report": "2026-02-15T10:00:00Z",
                }
            ],
            "status_counts": {
                "total": 0,
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
                "unknown": 0,
            },
            "panel_info": {"panels": 1},
            "status_type_counts": {"ok": 1},
            "connectivity_state": " ",
        },
    )

    snapshot = sensor_mod._microinverter_inventory_snapshot(coord)
    assert snapshot["total_inverters"] == 2
    assert snapshot["unknown_inverters"] == 2
    assert snapshot["latest_reported_device"]["serial_number"] == "INV-A"
    assert snapshot["panel_info"] == {"panels": 1}
    assert snapshot["status_type_counts"] == {"ok": 1}
    assert snapshot["connectivity_state"] == "unknown"
    assert sensor_mod._microinverter_connectivity_state({"total_inverters": 0}) is None


def test_microinverter_reporting_count_attributes_fallbacks(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseMicroinverterReportingCountSensor,
    )

    class BadInt:
        def __int__(self) -> int:
            raise ValueError("bad")

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_view.type_bucket = lambda _type_key: {  # type: ignore[assignment]
        "count": BadInt(),
        "type_label": " ",
        "devices": [{"serial_number": "INV-A"}],
    }
    coord.inventory_view.type_label = lambda _type_key: "Microinverters Fallback"  # type: ignore[assignment]

    sensor = EnphaseMicroinverterReportingCountSensor(coord)
    attrs = sensor.extra_state_attributes
    assert attrs["device_count"] == 1
    assert attrs["type_label"] == "Microinverters Fallback"
    assert "devices" in sensor._unrecorded_attributes
    assert "panel_info" in sensor._unrecorded_attributes

    coord.inventory_view.type_label = lambda _type_key: " "  # type: ignore[assignment]
    attrs = EnphaseMicroinverterReportingCountSensor(coord).extra_state_attributes
    assert attrs["type_label"] == "Microinverters"


def test_microinverter_sensor_available_branches(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseMicroinverterConnectivityStatusSensor,
        EnphaseMicroinverterLastReportedSensor,
        EnphaseMicroinverterReportingCountSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "microinverter": {
                "type_key": "microinverter",
                "count": 1,
                "devices": [{"serial_number": "INV-A"}],
                "status_counts": {"total": 1, "not_reporting": 0},
            }
        },
        ["microinverter"],
    )
    coord.last_update_success = False
    coord.last_success_utc = None

    assert EnphaseMicroinverterConnectivityStatusSensor(coord).available is False
    assert EnphaseMicroinverterReportingCountSensor(coord).available is False
    assert EnphaseMicroinverterLastReportedSensor(coord).available is False

    coord.last_update_success = True
    assert EnphaseMicroinverterConnectivityStatusSensor(coord).available is True
    assert EnphaseMicroinverterReportingCountSensor(coord).available is True
    assert EnphaseMicroinverterLastReportedSensor(coord).available is False


def test_heatpump_diagnostic_sensors_expose_inventory_and_power(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseHeatPumpConnectivityStatusSensor,
        EnphaseHeatPumpEnergyMeterSensor,
        EnphaseHeatPumpLastReportedSensor,
        EnphaseHeatPumpPowerSensor,
        EnphaseHeatPumpSgReadyModeSensor,
        EnphaseHeatPumpStatusSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 3,
                "devices": [
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG-1",
                        "name": "SG Ready Gateway",
                        "statusText": "Recommended",
                        "last_report": "2026-02-27T09:14:44Z",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-EM-1",
                        "name": "Energy Meter",
                        "statusText": "Warning",
                        "last_report": "2026-02-27T09:15:44Z",
                        "firmware_version": "3.3",
                    },
                    {
                        "device_type": "HEAT_PUMP",
                        "device_uid": "HP-1",
                        "name": "Heat Pump",
                        "statusText": "Normal",
                        "model": "Europa Mini WP",
                    },
                ],
                "status_counts": {
                    "total": 3,
                    "normal": 2,
                    "warning": 1,
                    "error": 0,
                    "not_reporting": 0,
                    "unknown": 0,
                },
                "status_summary": "Normal 2 | Warning 1 | Error 0 | Not Reporting 0",
                "overall_status_text": "Normal",
                "latest_reported_utc": "2026-02-27T09:15:44+00:00",
                "latest_reported_device": {
                    "device_type": "ENERGY_METER",
                    "device_uid": "HP-EM-1",
                },
                "device_type_counts": {
                    "HEAT_PUMP": 1,
                    "ENERGY_METER": 1,
                    "SG_READY_GATEWAY": 1,
                },
                "model_summary": "Europa Mini WP x1",
                "firmware_summary": "3.3 x1",
            }
        },
        ["heatpump"],
    )
    coord._heatpump_power_w = 863.2  # noqa: SLF001
    coord._heatpump_power_sample_utc = datetime(
        2026, 2, 27, 9, 15, tzinfo=timezone.utc
    )  # noqa: SLF001
    coord._heatpump_power_start_utc = datetime(
        2026, 2, 27, 0, 0, tzinfo=timezone.utc
    )  # noqa: SLF001
    coord._heatpump_power_device_uid = "HP-1"  # noqa: SLF001
    coord._heatpump_power_source = "hems_energy_consumption_delta:HP-1"  # noqa: SLF001
    coord._heatpump_power_raw_w = 0.0  # noqa: SLF001
    coord._heatpump_power_window_seconds = 1200.0  # noqa: SLF001
    coord._heatpump_power_validation = "smoothed_idle_delta"  # noqa: SLF001
    coord._heatpump_power_smoothed = True  # noqa: SLF001
    coord._heatpump_power_last_error = None  # noqa: SLF001
    coord.heatpump_runtime._heatpump_power_last_success_utc = datetime(  # noqa: SLF001
        2026, 2, 27, 9, 16, tzinfo=timezone.utc
    )
    coord.heatpump_runtime._heatpump_power_using_stale = True  # noqa: SLF001
    coord._heatpump_power_last_success_utc = datetime(  # noqa: SLF001
        2026, 2, 27, 9, 16, tzinfo=timezone.utc
    )
    coord._heatpump_power_using_stale = True  # noqa: SLF001
    coord.heatpump_state._heatpump_power_last_success_utc = datetime(  # noqa: SLF001
        2026, 2, 27, 9, 16, tzinfo=timezone.utc
    )
    coord.heatpump_state._heatpump_power_using_stale = True  # noqa: SLF001
    coord._heatpump_runtime_state = {  # noqa: SLF001
        "device_uid": "HP-1",
        "heatpump_status": "RUNNING",
        "sg_ready_mode_raw": "MODE_3",
        "sg_ready_mode_label": "Recommended",
        "sg_ready_active": True,
        "sg_ready_contact_state": "closed",
        "vpp_sgready_mode_override": "NONE",
        "last_report_at": "2026-02-27T09:15:59Z",
        "source": "hems_heatpump_state:HP-1",
    }
    coord._heatpump_runtime_state_last_error = None  # noqa: SLF001
    coord._hems_devices_last_success_utc = datetime(
        2026, 2, 27, 9, 16, tzinfo=timezone.utc
    )  # noqa: SLF001
    coord._hems_devices_last_success_mono = time.monotonic() - 12  # noqa: SLF001
    coord._hems_devices_using_stale = True  # noqa: SLF001

    status_sensor = EnphaseHeatPumpStatusSensor(coord)
    assert status_sensor.native_value == "Running"
    status_attrs = status_sensor.extra_state_attributes
    assert status_attrs["device_uid"] == "HP-1"
    assert status_attrs["heatpump_status_raw"] == "RUNNING"
    assert status_attrs["sg_ready_mode_raw"] == "MODE_3"
    assert status_attrs["sg_ready_active"] is True
    assert status_attrs["sg_ready_contact_state"] == "closed"
    assert status_attrs["last_report_at_utc"] == "2026-02-27T09:15:59+00:00"

    connectivity_sensor = EnphaseHeatPumpConnectivityStatusSensor(coord)
    assert connectivity_sensor.native_value == "Normal"
    connectivity_attrs = connectivity_sensor.extra_state_attributes
    assert connectivity_attrs["total_devices"] == 3
    assert connectivity_attrs["status_counts"]["warning"] == 1
    assert connectivity_attrs["device_type_counts"]["HEAT_PUMP"] == 1
    assert connectivity_attrs["hems_data_stale"] is True
    assert connectivity_attrs["hems_last_success_utc"] == "2026-02-27T09:16:00+00:00"
    assert connectivity_attrs["hems_last_success_age_s"] is not None

    sg_sensor = EnphaseHeatPumpSgReadyModeSensor(coord)
    assert sg_sensor.native_value == "Recommended"
    sg_attrs = sg_sensor.extra_state_attributes
    assert sg_attrs["heatpump_status_raw"] == "RUNNING"
    assert sg_attrs["sg_ready_mode_raw"] == "MODE_3"
    assert sg_attrs["sg_ready_mode"] == 3
    assert sg_attrs["sg_ready_contact_state"] == "closed"
    assert sg_attrs["status_explanation"] == (
        "Recommended means the SG Ready contact is closed."
    )

    meter_sensor = EnphaseHeatPumpEnergyMeterSensor(coord)
    assert meter_sensor.native_value == "Warning"
    meter_attrs = meter_sensor.extra_state_attributes
    assert meter_attrs["device_type"] == "ENERGY_METER"
    assert meter_attrs["member_count"] == 1
    assert meter_attrs["members"][0]["firmware_version"] == "3.3"

    last_reported_sensor = EnphaseHeatPumpLastReportedSensor(coord)
    assert last_reported_sensor.available is True
    assert last_reported_sensor.native_value == datetime(
        2026, 2, 27, 9, 15, 59, tzinfo=timezone.utc
    )
    assert last_reported_sensor.extra_state_attributes == {}

    power_sensor = EnphaseHeatPumpPowerSensor(coord)
    assert power_sensor.available is True
    assert power_sensor.native_value == pytest.approx(863.2)
    power_attrs = power_sensor.extra_state_attributes
    assert power_attrs["device_uid"] == "HP-1"
    assert power_attrs["source"] == "hems_energy_consumption_delta:HP-1"
    assert power_attrs["raw_power_w"] == pytest.approx(0.0)
    assert power_attrs["power_window_seconds"] == pytest.approx(1200.0)
    assert power_attrs["power_validation"] == "smoothed_idle_delta"
    assert power_attrs["smoothed"] is True
    assert "using_stale" in power_attrs
    assert "last_success_utc" in power_attrs


def test_heatpump_power_sensor_unavailable_without_sample(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import EnphaseHeatPumpPowerSensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "count": 1,
                "devices": [{"device_type": "HEAT_PUMP", "device_uid": "HP-1"}],
            }
        },
        ["heatpump"],
    )
    coord._heatpump_power_w = None  # noqa: SLF001
    coord._heatpump_power_last_error = "fetch failed"  # noqa: SLF001

    sensor = EnphaseHeatPumpPowerSensor(coord)
    assert sensor.available is False
    assert sensor.native_value is None


def test_heatpump_helper_edge_paths(coordinator_factory) -> None:
    assert sensor_mod._heatpump_member_device_type(None) is None
    assert sensor_mod._heatpump_member_device_type({"device_type": " "}) is None
    assert sensor_mod._heatpump_member_status_text(None) is None
    assert sensor_mod._heatpump_member_status_text({"status": ""}) is None
    assert (
        sensor_mod._heatpump_member_status_text({"status": "not_reporting"})
        == "Not Reporting"
    )
    assert sensor_mod._heatpump_member_last_reported(None) is None
    assert sensor_mod._heatpump_member_last_reported({"name": "hp"}) is None
    assert sensor_mod._heatpump_worst_status_text({"error": 1}) == "Error"
    assert sensor_mod._heatpump_worst_status_text({"warning": 1}) == "Warning"
    assert (
        sensor_mod._heatpump_worst_status_text({"not_reporting": 1}) == "Not Reporting"
    )
    assert sensor_mod._heatpump_worst_status_text({"unknown": 1}) == "Unknown"
    assert sensor_mod._heatpump_worst_status_text({"normal": 1}) == "Normal"
    assert sensor_mod._heatpump_worst_status_text({}) is None

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "type_key": "heatpump",
            "type_label": "Heat Pump",
            "count": "bad-count",
            "status_counts": {
                "total": "bad",
                "normal": 0,
                "warning": 0,
                "error": 0,
                "not_reporting": 0,
                "unknown": 0,
            },
            "devices": [
                {
                    "device_type": "HEAT_PUMP",
                    "name": "HP no report",
                    "status": "normal",
                },
                {
                    "device-type": "SG_READY_GATEWAY",
                    "device-uid": "HP-SG-1",
                    "name": "SG",
                    "status": "normal",
                    "last-report": "2026-02-27T09:14:44Z",
                },
            ],
            "device_type_counts": {
                None: 1,
                "SG_READY_GATEWAY": "bad",
                "ENERGY_METER": 0,
            },
            "status_summary": "",
        }
    }
    coord._type_device_order = ["heatpump"]  # noqa: SLF001
    coord._devices_inventory_ready = True  # noqa: SLF001
    snapshot = sensor_mod._heatpump_snapshot(coord)
    assert snapshot["total_devices"] == 2
    assert snapshot["without_last_report_count"] == 1
    assert snapshot["latest_reported_device"]["device_uid"] == "HP-SG-1"
    assert snapshot["overall_status_text"] == "Normal"
    assert snapshot["device_type_counts"] == {}
    assert snapshot["status_summary"].startswith("Normal")

    coord._type_device_buckets = {  # noqa: SLF001
        "heatpump": {
            "type_key": "heatpump",
            "type_label": "Heat Pump",
            "count": 0,
            "devices": [
                {"device_type": "ENERGY_METER", "status": "normal"},
                {"device_type": "ENERGY_METER", "status": "warning"},
            ],
        }
    }
    coord._type_device_order = ["heatpump"]  # noqa: SLF001
    snapshot2 = sensor_mod._heatpump_snapshot(coord)
    assert snapshot2["total_devices"] == 2
    assert snapshot2["overall_status_text"] == "Warning"
    assert snapshot2["device_type_counts"]["ENERGY_METER"] == 2

    type_snapshot = sensor_mod._heatpump_type_snapshot(
        coord, device_type="ENERGY_METER"
    )
    assert type_snapshot["member_count"] == 2
    assert type_snapshot["native_status"] == "Warning"


def test_heatpump_sensor_availability_edge_paths(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseHeatPumpConnectivityStatusSensor,
        EnphaseHeatPumpLastReportedSensor,
        EnphaseHeatPumpPowerSensor,
        EnphaseHeatPumpSgReadyModeSensor,
        EnphaseHeatPumpStatusSensor,
        EnphaseSiteEnergySensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [
                    {"device_type": "SG_READY_GATEWAY", "device_uid": "HP-SG-1"}
                ],
            }
        },
        ["heatpump"],
    )

    coord.last_update_success = False
    coord.last_success_utc = None
    assert EnphaseHeatPumpStatusSensor(coord).available is False
    assert EnphaseHeatPumpSgReadyModeSensor(coord).available is False
    assert EnphaseHeatPumpLastReportedSensor(coord).available is False
    assert EnphaseHeatPumpPowerSensor(coord).available is False
    assert EnphaseHeatPumpConnectivityStatusSensor(coord).available is False

    coord.last_update_success = True
    coord._devices_inventory_ready = True  # noqa: SLF001
    assert EnphaseHeatPumpStatusSensor(coord).available is False
    assert EnphaseHeatPumpSgReadyModeSensor(coord).available is False
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 0,
                "devices": [],
            }
        },
        ["heatpump"],
    )
    coord._devices_inventory_ready = False  # noqa: SLF001
    coord._heatpump_runtime_state = {  # noqa: SLF001
        "device_uid": "HP-1",
        "heatpump_status": "IDLE",
        "sg_ready_mode_raw": "MODE_2",
        "sg_ready_mode_label": "Normal",
        "last_report_at": "2026-02-27T09:00:00Z",
    }
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
    assert EnphaseHeatPumpStatusSensor(coord).available is True

    coord._heatpump_power_last_error = "fetch failed"  # noqa: SLF001
    power_attrs = EnphaseHeatPumpPowerSensor(coord).extra_state_attributes
    assert power_attrs["last_error"] == "fetch failed"

    coord.energy.site_energy = {
        "heat_pump": {
            "value_kwh": 1.0,
            "bucket_count": 1,
            "fields_used": ["heatpump"],
            "start_date": "2026-02-27",
            "source_unit": "Wh",
        }
    }
    monkeypatch.setattr(
        type(coord),
        "heatpump_power_w",
        property(lambda _self: "bad-float"),
    )
    site_sensor = EnphaseSiteEnergySensor(
        coord,
        "heat_pump",
        "site_heat_pump_consumption",
        "Site Heat Pump Consumption",
    )
    assert site_sensor.extra_state_attributes["heat_pump_power_w"] is None


def test_site_heat_pump_energy_attributes_without_daily_snapshot(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseSiteEnergySensor

    coord = coordinator_factory(serials=[])
    coord.energy.site_energy = {
        "heat_pump": {
            "value_kwh": 0.75,
            "last_report_date": "2026-02-27T09:15:00Z",
        }
    }
    monkeypatch.setattr(
        type(coord),
        "heatpump_daily_consumption",
        property(lambda _self: None),
    )

    sensor = EnphaseSiteEnergySensor(
        coord,
        "heat_pump",
        "site_heat_pump_consumption",
        "Site Heat Pump Consumption",
    )

    assert sensor.extra_state_attributes == {
        "sampled_at_utc": "2026-02-27T09:15:00+00:00"
    }


def test_heatpump_runtime_sensor_uid_fallback_and_error_paths(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import (
        _heatpump_runtime_snapshot,
        _heatpump_sg_ready_semantics,
        EnphaseHeatPumpConnectivityStatusSensor,
        EnphaseHeatPumpEnergyMeterSensor,
        EnphaseHeatPumpLastReportedSensor,
        EnphaseHeatPumpSgReadyModeSensor,
        EnphaseHeatPumpStatusSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.last_update_success = True
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
        "device_uid": " HP-1 ",
        "heatpump_status": "RUNNING",
        "sg_ready_mode_raw": "MODE_3",
        "sg_ready_mode_label": "Recommended",
    }

    monkeypatch.setattr(
        coord,
        "_heatpump_runtime_device_uid",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert sensor_mod._heatpump_runtime_device_uid(coord) is None
    assert EnphaseHeatPumpStatusSensor(coord).available is False
    assert EnphaseHeatPumpSgReadyModeSensor(coord).available is False

    monkeypatch.setattr(coord, "_heatpump_runtime_device_uid", None, raising=False)
    assert EnphaseHeatPumpStatusSensor(coord).available is True
    assert EnphaseHeatPumpSgReadyModeSensor(coord).available is True

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 0,
                "devices": [],
            }
        },
        ["heatpump"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    assert EnphaseHeatPumpConnectivityStatusSensor(coord).available is False
    assert EnphaseHeatPumpEnergyMeterSensor(coord).available is False
    assert (
        _heatpump_runtime_snapshot(SimpleNamespace(heatpump_runtime_state=None)) == {}
    )
    assert _heatpump_sg_ready_semantics("unexpected") == {}

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
    meter_sensor = EnphaseHeatPumpEnergyMeterSensor(coord)
    monkeypatch.setattr(meter_sensor, "_snapshot", lambda: {"member_count": 0})
    assert meter_sensor.available is False

    monkeypatch.setattr(
        coord.inventory_view,
        "has_type_for_entities",
        lambda _type_key: False,
    )
    assert meter_sensor.available is False
    monkeypatch.setattr(
        coord.inventory_view,
        "has_type_for_entities",
        lambda _type_key: True,
    )

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 0,
                "devices": [],
            }
        },
        ["heatpump"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    assert meter_sensor.available is False

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"device_type": "ENERGY_METER", "device_uid": "HP-EM-1"}],
            }
        },
        ["heatpump"],
    )
    coord._heatpump_runtime_state = {  # noqa: SLF001
        "device_uid": "HP-1",
        "last_report_at": "2026-02-27T09:00:00Z",
    }
    assert EnphaseHeatPumpLastReportedSensor(coord).available is False


def test_heatpump_connectivity_sensor_availability_paths(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseHeatPumpConnectivityStatusSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.last_update_success = True
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

    sensor = EnphaseHeatPumpConnectivityStatusSensor(coord)
    assert sensor.available is True

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 0,
                "devices": [],
            }
        },
        ["heatpump"],
    )
    coord._devices_inventory_ready = False  # noqa: SLF001
    assert sensor.available is True

    coord._devices_inventory_ready = True  # noqa: SLF001
    assert sensor.available is False


def test_heatpump_snapshot_fallback_covers_summary_error_and_type_snapshot_paths() -> (
    None
):
    mono_now = time.monotonic()
    coord = SimpleNamespace(
        heatpump_inventory_summary=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        heatpump_type_summary=lambda _device_type: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
        type_bucket=lambda _type_key: {
            "count": "bad-count",
            "devices": [
                {
                    "device-type": "HEAT_PUMP",
                    "name": "Heat Pump",
                    "status": "not_reporting",
                    "last-report": "2026-02-15T10:00:00Z",
                },
                {
                    "device_type": "ENERGY_METER",
                    "name": "Meter 1",
                    "status": "warning",
                    "device_uid": "EM-1",
                    "last_reported": "2026-02-15T10:05:00Z",
                },
                {
                    "device_type": "ENERGY_METER",
                    "name": "Meter 2",
                    "status_text": "Normal",
                    "device-uid": "EM-2",
                    "last_reported_at": "2026-02-15T10:06:00Z",
                },
                {"device_type": "", "name": "Unknown Member"},
                "bad-member",
            ],
            "status_counts": {"total": "bad"},
            "overall_status_text": " ",
            "device_type_counts": {
                None: 1,
                "ENERGY_METER": "2",
                "SG_READY_GATEWAY": "bad",
                "ZERO": 0,
            },
            "status_summary": " ",
            "model_summary": "Model A x1",
            "firmware_summary": "FW x1",
        },
        _hems_devices_last_success_utc=datetime(
            2026, 2, 15, 10, 7, tzinfo=timezone.utc
        ),
        _hems_devices_last_success_mono=mono_now - 1.6,
        _hems_devices_using_stale=True,
    )

    snapshot = sensor_mod._heatpump_snapshot(coord)
    assert snapshot["total_devices"] == 4
    assert snapshot["without_last_report_count"] == 1
    assert snapshot["latest_reported_device"] == {
        "device_type": "ENERGY_METER",
        "name": "Meter 2",
        "device_uid": "EM-2",
        "status": "Normal",
    }
    assert snapshot["overall_status_text"] == "Not Reporting"
    assert snapshot["device_type_counts"] == {"ENERGY_METER": 2}
    assert snapshot["status_counts"] == {
        "total": 4,
        "normal": 1,
        "warning": 1,
        "error": 0,
        "not_reporting": 1,
        "unknown": 1,
    }
    assert snapshot["status_summary"] == (
        "Normal 1 | Warning 1 | Error 0 | Not Reporting 1 | Unknown 1"
    )
    assert snapshot["model_summary"] == "Model A x1"
    assert snapshot["firmware_summary"] == "FW x1"
    assert snapshot["hems_data_stale"] is True
    assert snapshot["hems_last_success_utc"] == "2026-02-15T10:07:00+00:00"
    assert isinstance(snapshot["hems_last_success_age_s"], float)

    energy_meter_snapshot = sensor_mod._heatpump_type_snapshot(
        coord, device_type="energy_meter"
    )
    assert energy_meter_snapshot["device_type"] == "ENERGY_METER"
    assert energy_meter_snapshot["member_count"] == 2
    assert energy_meter_snapshot["latest_reported_device"] == {
        "name": "Meter 2",
        "device_uid": "EM-2",
        "status": "Normal",
    }
    assert energy_meter_snapshot["native_status"] == "Warning"
    assert energy_meter_snapshot["status_summary"] == (
        "Normal 1 | Warning 1 | Error 0 | Not Reporting 0"
    )
    assert energy_meter_snapshot["hems_data_stale"] is True

    heat_pump_type_snapshot = sensor_mod._heatpump_type_snapshot(
        coord, device_type="heat_pump"
    )
    assert heat_pump_type_snapshot["member_count"] == 1
    assert heat_pump_type_snapshot["native_status"] == "Not Reporting"


def test_heatpump_snapshot_fallback_covers_remaining_edge_paths() -> None:
    coord = SimpleNamespace(
        heatpump_inventory_summary=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        heatpump_type_summary=lambda _device_type: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
        type_bucket=lambda _type_key: {
            "count": 0,
            "devices": [
                {"device_type": "ENERGY_METER", "name": "Meter A", "status": "warning"},
                {
                    "device_type": "SG_READY_GATEWAY",
                    "name": "Gateway",
                    "status": "normal",
                    "last_report": "2026-02-15T10:00:00Z",
                },
            ],
            "status_counts": {
                "total": 2,
                "normal": 1,
                "warning": 1,
                "error": 0,
                "not_reporting": 0,
                "unknown": 0,
            },
        },
        _hems_devices_last_success_utc="bad",
        _hems_devices_last_success_mono=None,
        _hems_devices_using_stale=False,
    )

    snapshot = sensor_mod._heatpump_snapshot(coord)
    assert snapshot["total_devices"] == 2
    assert snapshot["without_last_report_count"] == 1
    assert snapshot["overall_status_text"] == "Warning"
    assert snapshot["device_type_counts"] == {"ENERGY_METER": 1, "SG_READY_GATEWAY": 1}
    assert snapshot["hems_last_success_utc"] is None

    type_snapshot = sensor_mod._heatpump_type_snapshot(
        coord, device_type="energy_meter"
    )
    assert type_snapshot["member_count"] == 1
    assert type_snapshot["latest_reported_device"] is None
    assert type_snapshot["native_status"] == "Warning"


def test_gateway_meter_sensors_expose_status_and_meter_attributes(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConsumptionMeterSensor,
        EnphaseGatewayProductionMeterSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 3,
                "devices": [
                    {
                        "name": "Production Meter",
                        "serial_number": "MTR-P",
                        "channel_type": "production_meter",
                        "statusText": "Normal",
                        "connected": "true",
                        "ip": "192.0.2.10",
                        "last_report": "2026-02-15T10:00:00Z",
                        "envoy_sw_version": "8.3.1",
                    },
                    {
                        "name": "Consumption Meter",
                        "serial_number": "MTR-C",
                        "channel_type": "consumption_meter",
                        "status": "NOT_REPORTING",
                        "connected": 0,
                        "ip": "192.0.2.11",
                    },
                    {"name": "System Controller", "channel_type": "enpower"},
                ],
            }
        },
        ["envoy"],
    )
    coord._system_dashboard_devices_details_raw = {  # noqa: SLF001
        "envoy": {
            "meters": {
                "meters": [
                    {
                        "name": "Production Meter",
                        "serial_number": "MTR-P",
                        "meter_type": "Production",
                        "meter_state": "Enabled",
                        "config_type": "Production",
                    },
                    {
                        "name": "Consumption Meter",
                        "serial_number": "MTR-C",
                        "meter_type": "Consumption",
                        "meter_state": "Enabled",
                        "config_type": "Net (Load with Solar)",
                        "last_report": "2026-02-15T11:00:00Z",
                    },
                ]
            }
        }
    }

    production = EnphaseGatewayProductionMeterSensor(coord)
    assert production.native_value == "Normal"
    assert production.entity_registry_enabled_default is False
    p_attrs = production.extra_state_attributes
    assert p_attrs["meter_name"] == "Production Meter"
    assert p_attrs["meter_type"] == "production"
    assert p_attrs["dashboard_meter_type"] == "Production"
    assert p_attrs["channel_type"] == "production_meter"
    assert p_attrs["connected"] is True
    assert p_attrs["envoy_sw_version"] == "8.3.1"
    assert p_attrs["meter_state"] == "Enabled"
    assert p_attrs["config_type"] == "Production"
    assert p_attrs["meter_attributes"]["serial_number"] == "MTR-P"
    assert p_attrs["last_reported_utc"] is not None

    consumption = EnphaseGatewayConsumptionMeterSensor(coord)
    assert consumption.native_value == "Not Reporting"
    assert consumption.entity_registry_enabled_default is False
    c_attrs = consumption.extra_state_attributes
    assert c_attrs["meter_name"] == "Consumption Meter"
    assert c_attrs["meter_type"] == "consumption"
    assert c_attrs["dashboard_meter_type"] == "Consumption"
    assert c_attrs["channel_type"] == "consumption_meter"
    assert c_attrs["connected"] is False
    assert c_attrs["meter_state"] == "Enabled"
    assert c_attrs["config_type"] == "Net (Load with Solar)"
    assert c_attrs["last_reported_utc"] == "2026-02-15T11:00:00+00:00"
    assert c_attrs["meter_attributes"]["serial_number"] == "MTR-C"
    assert "meter_attributes" in consumption._unrecorded_attributes  # noqa: SLF001
    assert "last_reported_utc" in consumption._unrecorded_attributes  # noqa: SLF001


def test_gateway_meter_sensor_name_fallback_and_missing_member(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConsumptionMeterSensor,
        EnphaseGatewayProductionMeterSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [
                    {
                        "name": "Production Meter",
                        "serial_number": "MTR-P",
                        "status": "NORMAL",
                    }
                ],
            }
        },
        ["envoy"],
    )

    production = EnphaseGatewayProductionMeterSensor(coord)
    assert production.available is True
    assert production.native_value == "Normal"

    consumption = EnphaseGatewayConsumptionMeterSensor(coord)
    assert consumption.available is False
    assert consumption.native_value is None
    assert consumption.extra_state_attributes == {}

    coord.last_update_success = False
    assert production.available is False


def test_gateway_last_reported_sensor_uses_dashboard_fallback(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseGatewayLastReportedSensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [
                    {
                        "name": "IQ Gateway",
                        "serial_number": "GW-1",
                        "statusText": "Normal",
                    }
                ],
            }
        },
        ["envoy"],
    )
    coord._system_dashboard_devices_details_raw = {  # noqa: SLF001
        "envoy": {
            "envoys": {
                "envoys": [
                    {
                        "name": "IQ Gateway",
                        "serial_number": "GW-1",
                        "statusText": "Normal",
                        "last_report": "2026-02-15T12:00:00Z",
                    }
                ]
            }
        }
    }

    sensor = EnphaseGatewayLastReportedSensor(coord)
    assert sensor.available is True
    assert sensor.native_value == datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)


def test_battery_storage_sensors_include_dashboard_detail_fields(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseBatteryStorageChargeSensor,
        EnphaseBatteryStorageStatusSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._battery_storage_data = {  # noqa: SLF001
        "BAT-1": {
            "identity": "BAT-1",
            "name": "IQ Battery 5P",
            "serial_number": "BAT-1",
            "current_charge_pct": 48.0,
            "led_status": 12,
            "status": "normal",
            "status_text": "Normal",
            "status_normalized": "normal",
        }
    }
    coord._battery_storage_order = ["BAT-1"]  # noqa: SLF001
    coord._system_dashboard_devices_details_raw = {  # noqa: SLF001
        "encharge": {
            "encharges": {
                "encharges": [
                    {
                        "serial_number": "BAT-1",
                        "phase": "L1(A)",
                        "operation_mode": "Multi-mode On Grid, Charging",
                        "app_version": "3.0.8557_rel/31.44",
                        "sw_version": "546-00002-01-v01",
                        "rssi_subghz": 1,
                        "rssi_24ghz": 5,
                        "rssi_dbm": -61,
                        "led_status": 12,
                        "alarm_id": None,
                        "current_charge_pct": 99.0,
                        "status": "warning",
                    }
                ]
            }
        }
    }

    charge = EnphaseBatteryStorageChargeSensor(coord, "BAT-1")
    status = EnphaseBatteryStorageStatusSensor(coord, "BAT-1")

    assert charge.native_value == 48.0
    assert status.native_value == "charging"
    attrs = charge.extra_state_attributes
    assert attrs == {
        "serial_number": "BAT-1",
        "status": "normal",
        "sampled_at_utc": None,
    }
    assert set(attrs) <= charge._unrecorded_attributes  # noqa: SLF001
    assert status.extra_state_attributes["state"] == 12


def test_gateway_iq_energy_router_sensor_state_and_attributes(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseGatewayIQEnergyRouterSensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._hems_devices_payload = {  # noqa: SLF001
        "data": {
            "hems-devices": {
                "gateway": [
                    {
                        "name": "IQ Energy Router_1",
                        "device-type": "IQ_ENERGY_ROUTER",
                        "uid": "LGX-025",
                        "device-uid": "5956621_IQ_ENERGY_ROUTER_1",
                        "statusText": "Normal",
                        "status": "normal",
                        "make": "Hive",
                        "model": "Nano Hub 2",
                        "pairing-status": "PAIRED",
                        "device-state": "ACTIVE",
                        "iqer-uid": "5956621_IQ_ENERGY_ROUTER_1",
                        "hems-device-id": "router-id",
                        "hems-device-facet-id": "router-facet-id",
                        "ip-address": "192.0.2.99",
                        "last-report": "2026-02-27T09:14:41Z",
                        "created-at": "2025-08-11T08:11:08Z",
                    },
                    {
                        "name": "IQ Gateway_1",
                        "device-type": "IQ_GATEWAY",
                    },
                ]
            }
        }
    }
    records = sensor_mod._gateway_iq_energy_router_records(coord)
    assert len(records) == 1
    assert records[0]["key"] == "5956621_iq_energy_router_1"

    sensor = EnphaseGatewayIQEnergyRouterSensor(
        coord,
        "5956621_iq_energy_router_1",
        1,
    )
    assert sensor.entity_registry_enabled_default is False
    assert sensor.available is True
    assert sensor.native_value == "Normal"
    attrs = sensor.extra_state_attributes
    assert attrs["name"] == "IQ Energy Router_1"
    assert attrs["status_text"] == "Normal"
    assert attrs["device_type"] == "IQ_ENERGY_ROUTER"
    assert attrs["uid"] == "LGX-025"
    assert attrs["device_uid"] == "5956621_IQ_ENERGY_ROUTER_1"
    assert attrs["make"] == "Hive"
    assert attrs["model"] == "Nano Hub 2"
    assert attrs["pairing_status"] == "PAIRED"
    assert attrs["device_state"] == "ACTIVE"
    assert attrs["iqer_uid"] == "5956621_IQ_ENERGY_ROUTER_1"
    assert attrs["hems_device_id"] == "router-id"
    assert attrs["hems_device_facet_id"] == "router-facet-id"
    assert attrs["last_reported_utc"] == "2026-02-27T09:14:41+00:00"
    assert attrs["ip_address"] == "192.0.2.99"
    assert attrs["created_at"] == "2025-08-11T08:11:08Z"
    assert sensor.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:envoy")
    }

    coord._hems_devices_payload = {"data": {"hems-devices": {}}}  # noqa: SLF001
    assert sensor.name == "IQ Energy Router_1"
    assert sensor.available is False
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


@pytest.mark.asyncio
async def test_async_setup_entry_adds_gateway_iq_energy_router_entities(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayIQEnergyRouterSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._hems_devices_payload = {  # noqa: SLF001
        "data": {
            "hems-devices": {
                "gateway": [
                    {
                        "name": "IQ Energy Router_1",
                        "device-type": "IQ_ENERGY_ROUTER",
                        "device-uid": "5956621_IQ_ENERGY_ROUTER_1",
                        "statusText": "Normal",
                    },
                    {
                        "device-type": "IQ_ENERGY_ROUTER",
                        "uid": "LGX-026",
                        "status": "warning",
                    },
                    {
                        "name": "Retired via status",
                        "device-type": "IQ_ENERGY_ROUTER",
                        "uid": "LGX-RETIRED-1",
                        "status": "retired",
                    },
                    {
                        "name": "Retired via flag",
                        "device-type": "IQ_ENERGY_ROUTER",
                        "uid": "LGX-RETIRED-2",
                        "isRetired": True,
                    },
                ]
            }
        }
    }
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    router_entities = [
        entity
        for entity in added
        if isinstance(entity, EnphaseGatewayIQEnergyRouterSensor)
    ]
    assert len(router_entities) == 2
    assert {entity.unique_id for entity in router_entities} == {
        f"enphase_ev_site_{coord.site_id}_gateway_iq_energy_router_5956621_iq_energy_router_1",
        f"enphase_ev_site_{coord.site_id}_gateway_iq_energy_router_lgx_026",
    }
    assert not any("retired" in entity.unique_id for entity in router_entities)
    assert router_entities[0].device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:envoy")
    }
    assert any(entity.native_value == "Warning" for entity in router_entities)
    fallback_name_entity = next(
        entity for entity in router_entities if entity.unique_id.endswith("_lgx_026")
    )
    assert fallback_name_entity.name == "IQ Energy Router_2"


@pytest.mark.asyncio
async def test_async_setup_entry_skips_duplicate_gateway_iq_energy_router_entities_on_repeat_topology(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayIQEnergyRouterSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._hems_devices_payload = {  # noqa: SLF001
        "data": {
            "hems-devices": {
                "gateway": [
                    {
                        "name": "IQ Energy Router_1",
                        "device-type": "IQ_ENERGY_ROUTER",
                        "device-uid": "5956621_IQ_ENERGY_ROUTER_1",
                        "statusText": "Normal",
                    }
                ]
            }
        }
    }
    callbacks: list[Any] = []

    def _add_listener(callback):
        callbacks.append(callback)
        return lambda: None

    coord.async_add_topology_listener = _add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)
    router_entities = [
        entity
        for entity in added
        if isinstance(entity, EnphaseGatewayIQEnergyRouterSensor)
    ]
    assert len(router_entities) == 1

    sync_topology_cb = next(
        cb for cb in callbacks if cb.__name__ == "_async_sync_topology"
    )
    sync_topology_cb()

    router_entities = [
        entity
        for entity in added
        if isinstance(entity, EnphaseGatewayIQEnergyRouterSensor)
    ]
    assert len(router_entities) == 1


@pytest.mark.asyncio
async def test_async_setup_entry_adds_gateway_iq_energy_router_entities_without_envoy_type(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayIQEnergyRouterSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.inventory_runtime._set_type_device_buckets({}, [])  # noqa: SLF001
    coord._hems_devices_payload = {  # noqa: SLF001
        "data": {
            "hems-devices": {
                "gateway": [
                    {
                        "name": "IQ Energy Router_1",
                        "device-type": "IQ_ENERGY_ROUTER",
                        "device-uid": "5956621_IQ_ENERGY_ROUTER_1",
                        "statusText": "Normal",
                    }
                ]
            }
        }
    }
    coord.last_success_utc = datetime(2026, 3, 1, tzinfo=timezone.utc)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    router_entities = [
        entity
        for entity in added
        if isinstance(entity, EnphaseGatewayIQEnergyRouterSensor)
    ]
    assert len(router_entities) == 1
    assert router_entities[0].available is True
    assert router_entities[0].unique_id == (
        f"enphase_ev_site_{coord.site_id}_gateway_iq_energy_router_5956621_iq_energy_router_1"
    )
    assert router_entities[0].device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:envoy")
    }


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_stale_gateway_iq_energy_router_entity(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._devices_inventory_payload = {  # noqa: SLF001
        "result": [{"type": "envoy", "devices": [{"name": "IQ Gateway"}]}]
    }
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    stale_unique_id = (
        f"enphase_ev_site_{coord.site_id}_gateway_iq_energy_router_old_key"
    )
    fake_registry = SimpleNamespace(
        entities={
            "sensor.gateway_iq_energy_router_old_key": SimpleNamespace(
                domain="sensor",
                entity_id="sensor.gateway_iq_energy_router_old_key",
                platform="enphase_ev",
                config_entry_id=config_entry.entry_id,
                unique_id=stale_unique_id,
            )
        },
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_remove.assert_any_call(
        "sensor.gateway_iq_energy_router_old_key"
    )


@pytest.mark.asyncio
async def test_async_setup_entry_gateway_iq_energy_router_sync_handles_invalid_records(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayIQEnergyRouterSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._devices_inventory_payload = {  # noqa: SLF001
        "result": [{"type": "envoy", "devices": [{"name": "IQ Gateway"}]}]
    }
    callbacks: list[Any] = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_topology_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    router_records = [
        [
            {"key": " ", "index": 1},
            {"key": "router_a", "index": "invalid"},
        ],
        [],
    ]

    def _fake_router_records(_coord):
        if router_records:
            return router_records.pop(0)
        return []

    monkeypatch.setattr(
        sensor_mod,
        "_gateway_iq_energy_router_records",
        _fake_router_records,
    )

    expected_unique_id = (
        f"enphase_ev_site_{coord.site_id}_gateway_iq_energy_router_router_a"
    )
    fake_registry = SimpleNamespace(
        entities={
            "sensor.gateway_iq_energy_router_unknown": SimpleNamespace(
                domain="sensor",
                entity_id="sensor.gateway_iq_energy_router_unknown",
                platform="enphase_ev",
                config_entry_id=config_entry.entry_id,
                unique_id=None,
            )
        },
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(
            side_effect=lambda domain, platform, unique_id: (
                "sensor.gateway_iq_energy_router_router_a"
                if domain == "sensor"
                and platform == "enphase_ev"
                and unique_id == expected_unique_id
                else None
            )
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    router_entities = [
        entity
        for entity in added
        if isinstance(entity, EnphaseGatewayIQEnergyRouterSensor)
    ]
    assert len(router_entities) == 1
    assert router_entities[0].unique_id == expected_unique_id

    for cb in callbacks:
        cb()

    fake_registry.async_remove.assert_any_call(
        "sensor.gateway_iq_energy_router_router_a"
    )


def test_gateway_iq_energy_router_helpers_handle_malformed_inventory_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    assert sensor_mod._gateway_iq_energy_router_inventory_buckets({}) == []

    coord._hems_group_members = None  # type: ignore[assignment]  # noqa: SLF001
    coord._devices_inventory_payload = {  # noqa: SLF001
        "result": [
            {"devices": []},
            {"type": "envoy", "devices": []},
            {"type": "hemsDevices", "devices": {"gateway": []}},
            {
                "type": "hemsDevices",
                "devices": [
                    None,
                    {"gateway": {"device-type": "IQ_ENERGY_ROUTER"}},
                    {
                        "gateway": [
                            None,
                            {"device-type": "IQ_ENERGY_ROUTER", "uid": "LGX-099"},
                        ]
                    },
                ],
            },
        ]
    }
    records = sensor_mod._gateway_iq_energy_router_records(coord)
    assert len(records) == 1
    assert records[0]["key"] == "lgx_099"
    assert sensor_mod._gateway_iq_energy_router_record(coord, " ") is None
    assert (
        sensor_mod._gateway_iq_energy_router_last_reported(
            {"last-report": "not-a-date"}
        )
        is None
    )


def test_gateway_iq_energy_router_records_prefers_restored_helper(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_view.gateway_iq_energy_router_records = lambda: [  # type: ignore[assignment]
        {
            "device-type": "IQ_ENERGY_ROUTER",
            "device-uid": "RESTORED_ROUTER",
            "name": "Restored Router",
        }
    ]

    records = sensor_mod._gateway_iq_energy_router_records(coord)

    assert len(records) == 1
    assert records[0]["member"]["device-uid"] == "RESTORED_ROUTER"


def test_gateway_iq_energy_router_records_reads_live_hems_members(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_view.gateway_iq_energy_router_records = None  # type: ignore[assignment]
    coord._hems_group_members = lambda *_args: [  # type: ignore[assignment]  # noqa: SLF001
        {"device-type": "IQ_GATEWAY"},
        {"device_type": "IQ_ENERGY_ROUTER", "device-uid": "LIVE_ROUTER"},
    ]

    records = sensor_mod._gateway_iq_energy_router_records(coord)

    assert len(records) == 1
    assert records[0]["member"]["device-uid"] == "LIVE_ROUTER"


def test_gateway_iq_energy_router_helpers_cover_remaining_fallback_paths(
    coordinator_factory,
) -> None:
    coord = coordinator_factory(serials=[RANDOM_SERIAL])

    assert sensor_mod._gateway_iq_energy_router_inventory_buckets(
        {"result": [None, {"type": "hemsDevices", "devices": []}]}
    ) == [{"type": "hemsDevices", "devices": []}]

    coord.inventory_view.gateway_iq_energy_router_summary_records = lambda: None  # type: ignore[assignment]

    def _restored_boom():
        raise RuntimeError("restored failed")

    coord.inventory_view.gateway_iq_energy_router_records = _restored_boom  # type: ignore[assignment]
    coord._hems_group_members = None  # type: ignore[assignment]  # noqa: SLF001
    coord._devices_inventory_payload = {  # noqa: SLF001
        "value": {
            "result": [
                {"type": "   "},
                {"type": "envoy", "devices": []},
                {"type": "hemsDevices", "devices": {}},
                {
                    "type": "hemsDevices",
                    "devices": [
                        None,
                        {"gateway": {"device-type": "IQ_ENERGY_ROUTER"}},
                        {
                            "gateway": [
                                None,
                                {
                                    "device-type": "IQ_ENERGY_ROUTER",
                                    "device-uid": "RETIRED-1",
                                    "statusText": "retired",
                                },
                                {
                                    "device-type": "IQ_GATEWAY",
                                    "device-uid": "NOT-A-ROUTER",
                                },
                                {
                                    "device-type": "IQ_ENERGY_ROUTER",
                                    "device-uid": "ROUTER-1",
                                },
                                {
                                    "device-type": "IQ_ENERGY_ROUTER",
                                    "device-uid": "ROUTER-1",
                                },
                            ]
                        },
                    ],
                },
            ]
        }
    }

    records = sensor_mod._gateway_iq_energy_router_records(coord)
    assert [record["key"] for record in records] == ["router_1", "router_1_2"]

    coord.gateway_iq_energy_router_record = lambda _key: {"key": _key}  # type: ignore[assignment]
    assert sensor_mod._gateway_iq_energy_router_record(coord, "router_1") == {
        "key": "router_1"
    }

    coord.gateway_iq_energy_router_record = lambda _key: (_ for _ in ()).throw(  # type: ignore[assignment]
        RuntimeError("lookup failed")
    )
    assert (
        sensor_mod._gateway_iq_energy_router_record(coord, "router_1_2") == records[1]
    )


def test_gateway_iq_energy_router_sensor_name_and_availability_edge_paths(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseGatewayIQEnergyRouterSensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    sensor = EnphaseGatewayIQEnergyRouterSensor(coord, "router_a", 1)

    monkeypatch.setattr(
        sensor_mod,
        "_gateway_iq_energy_router_record",
        lambda *_args, **_kwargs: {"member": {"name": "Router Alpha"}},
    )
    assert sensor.name == "Router Alpha"

    monkeypatch.setattr(
        sensor_mod,
        "_gateway_iq_energy_router_record",
        lambda *_args, **_kwargs: {"member": "bad"},
    )
    assert sensor.extra_state_attributes == {}

    monkeypatch.setattr(
        sensor_mod.EnphaseGatewayIQEnergyRouterSensor,
        "platform",
        property(lambda _self: object()),
        raising=False,
    )
    monkeypatch.setattr(
        sensor_mod.SensorEntity,
        "name",
        property(lambda _self: "Localized Router"),
        raising=False,
    )
    assert sensor.name == "Localized Router"

    def _raise_name(_self):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        sensor_mod.SensorEntity,
        "name",
        property(_raise_name),
        raising=False,
    )
    assert sensor.name == "IQ Energy Router_1"

    coord.has_type_for_entities = lambda _type_key: False  # type: ignore[assignment]
    assert sensor.available is False


def test_system_controller_inventory_sensor_state_and_attributes(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseSystemControllerInventorySensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 2,
                "devices": [
                    {
                        "name": "System Controller",
                        "serial_number": "SC-1",
                        "channel_type": "enpower",
                        "statusText": "Normal",
                        "connected": True,
                        "MID": "Closed",
                        "MID N": "Closed",
                        "DER L1": "Closed",
                        "DER L2": "Closed",
                        "DER L3": "Closed",
                        "DER N": "Closed",
                        "NC1": "Energized",
                        "last_report": "2026-02-15T10:00:00Z",
                        "lastReportedAt": "2026-02-15T10:05:00Z",
                        "envoy_sw_version": "8.3.1",
                    },
                    {
                        "name": "Production Meter",
                        "channel_type": "production_meter",
                        "statusText": "Normal",
                    },
                ],
            }
        },
        ["envoy"],
    )

    sensor = EnphaseSystemControllerInventorySensor(coord)
    assert sensor.available is True
    assert sensor.native_value == "Normal"
    assert sensor.entity_registry_enabled_default is False
    attrs = sensor.extra_state_attributes
    assert attrs["name"] == "System Controller"
    assert attrs["channel_type"] == "enpower"
    assert attrs["status_text"] == "Normal"
    assert attrs["serial_number"] == "SC-1"
    assert attrs["connected"] is True
    assert attrs["envoy_sw_version"] == "8.3.1"
    assert attrs["mid"] == "Closed"
    assert attrs["mid_n"] == "Closed"
    assert attrs["der_l1"] == "Closed"
    assert attrs["terminal_descriptions"]["mid"] == (
        "Microgrid interconnection device line"
    )
    assert attrs["terminal_descriptions"]["mid_n"] == (
        "Microgrid interconnection device neutral"
    )
    assert attrs["terminal_descriptions"]["der_n"] == (
        "Distributed energy resource neutral"
    )
    assert attrs["terminal_descriptions"]["nc1"] == (
        "Load-control relay NC1 (normally closed)"
    )
    assert "last_reported_utc" in attrs
    assert "last_reported_at" not in attrs
    assert "last_reported_utc" in sensor._unrecorded_attributes  # noqa: SLF001


def test_system_controller_inventory_sensor_missing_member_unavailable(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseSystemControllerInventorySensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [
                    {"name": "Production Meter", "channel_type": "production_meter"}
                ],
            }
        },
        ["envoy"],
    )
    sensor = EnphaseSystemControllerInventorySensor(coord)
    assert sensor.available is False
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            }
        },
        ["envoy"],
    )
    coord.last_update_success = False
    assert sensor.available is False


def test_dry_contacts_inventory_sensor_state_and_attributes(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseDryContactsInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 2,
                "devices": [
                    {
                        "name": "Dry Contact 1",
                        "channel_type": "dry_contact_1",
                        "serial_number": "DC-1",
                        "statusText": "Closed",
                        "connected": True,
                        "NO1": "Open",
                        "relay_state": "closed",
                        "last_report": "2026-02-15T10:00:00Z",
                    },
                    {
                        "name": "Dry Contact 2",
                        "channel_type": "dry_contact_2",
                        "serial_number": "DC-2",
                        "statusText": "Open",
                        "connected": True,
                        "last_report": "2026-02-15T10:05:00Z",
                    },
                ],
            },
        },
        ["envoy", "dry_contact"],
    )

    sensor = EnphaseDryContactsInventorySensor(coord)
    assert sensor.available is True
    assert sensor.native_value == "Closed | Open"
    assert sensor.entity_registry_enabled_default is False
    assert sensor._attr_translation_key == "dry_contacts"  # noqa: SLF001
    attrs = sensor.extra_state_attributes
    assert attrs["name"] == "Dry Contacts"
    assert attrs["member_count"] == 2
    assert attrs["status_text"] == "Closed | Open"
    assert attrs["last_reported_utc"] is not None
    assert attrs["contacts"][0]["index"] == 1
    assert attrs["contacts"][0]["properties"]["name"] == "Dry Contact 1"
    assert attrs["contacts"][0]["terminal_descriptions"]["no1"] == (
        "Load-control relay NO1 (normally open)"
    )
    assert attrs["contacts"][1]["index"] == 2
    assert attrs["members"][0]["relay_state"] == "closed"
    assert "contacts" in sensor._unrecorded_attributes  # noqa: SLF001
    assert "members" in sensor._unrecorded_attributes  # noqa: SLF001


def test_dry_contacts_inventory_sensor_missing_member_unavailable(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseDryContactsInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            }
        },
        ["envoy"],
    )
    sensor = EnphaseDryContactsInventorySensor(coord)
    assert sensor.available is False
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_gateway_terminal_helpers_and_sg_ready_semantics() -> None:
    assert sensor_mod._gateway_terminal_descriptions(None) == {}
    assert sensor_mod._gateway_terminal_key(None) is None
    assert sensor_mod._gateway_terminal_values(None) == {}
    assert sensor_mod._heatpump_sg_ready_semantics(None) == {}
    assert sensor_mod._gateway_member_is_dry_contact({"channel_type": "NC1"}) is True
    assert (
        sensor_mod._gateway_member_is_dry_contact({"name": "Load-control relay NO2"})
        is True
    )
    assert sensor_mod._gateway_member_is_dry_contact({"channel_type": "envoy"}) is False

    member = {
        "MID": "Closed",
        "MID N": "   ",
        "DER L1": None,
        "NC1": True,
        "ignored": "value",
    }

    assert sensor_mod._gateway_terminal_key("MID N") == "mid_n"
    assert sensor_mod._gateway_terminal_descriptions(member) == {
        "mid": "Microgrid interconnection device line",
        "nc1": "Load-control relay NC1 (normally closed)",
    }
    assert sensor_mod._gateway_terminal_values(member) == {
        "mid": "Closed",
        "nc1": True,
    }
    assert sensor_mod._heatpump_sg_ready_semantics("Normal") == {
        "sg_ready_mode": 2,
        "sg_ready_contact_state": "open",
        "status_explanation": "Normal means the SG Ready contacts are open.",
    }


def test_dry_contacts_inventory_sensor_single_contact_adds_terminal_descriptions(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseDryContactsInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 1,
                "devices": [
                    {
                        "name": "Dry Contact 1",
                        "channel_type": "dry_contact_1",
                        "serial_number": "DC-1",
                        "statusText": "Closed",
                        "NO1": "Open",
                    }
                ],
            },
        },
        ["envoy", "dry_contact"],
    )

    attrs = EnphaseDryContactsInventorySensor(coord).extra_state_attributes
    assert attrs["no1"] == "Open"
    assert attrs["terminal_descriptions"]["no1"] == (
        "Load-control relay NO1 (normally open)"
    )


def test_dry_contacts_inventory_sensor_single_member_attributes(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseDryContactsInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 1,
                "devices": [
                    {
                        "name": "Dry Contact 1",
                        "channel_type": "dry_contact_1",
                        "serial_number": "DC-1",
                        "statusText": "Closed",
                        "connected": True,
                        "relay_state": "closed",
                    }
                ],
            },
        },
        ["envoy", "dry_contact"],
    )
    sensor = EnphaseDryContactsInventorySensor(coord)
    assert sensor.native_value == "Closed"
    attrs = sensor.extra_state_attributes
    assert attrs["channel_type"] == "dry_contact_1"
    assert attrs["serial_number"] == "DC-1"
    assert attrs["connected"] is True
    assert attrs["status_raw"] == "Closed"
    assert attrs["relay_state"] == "closed"
    assert attrs["contacts"][0]["in_use"] is None


def test_dry_contacts_inventory_sensor_merges_settings_into_contact_attributes(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseDryContactsInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 2,
                "devices": [
                    {
                        "name": "Dry Contact 1",
                        "channel_type": "dry_contact_1",
                        "serial_number": "DC-1",
                        "statusText": "Closed",
                    },
                    {
                        "name": "Dry Contact 2",
                        "channel_type": "dry_contact_2",
                        "serial_number": "DC-2",
                        "statusText": "Open",
                    },
                ],
            },
        },
        ["envoy", "dry_contact"],
    )
    coord.battery_runtime.parse_dry_contact_settings_payload(
        {
            "contacts": [
                {
                    "serial": "DC-1",
                    "displayName": "Solar Diverter",
                    "overrideSupported": True,
                    "overrideActive": False,
                    "controlMode": "schedule",
                    "pollingInterval": 30,
                    "socThreshold": 55,
                    "socThresholdMin": 20,
                    "socThresholdMax": 80,
                    "scheduleWindows": [{"startTime": "22:00", "endTime": "06:00"}],
                },
                {
                    "channelType": "dry_contact_9",
                    "name": "Unmatched Contact",
                    "overrideSupported": False,
                },
            ]
        }
    )

    attrs = EnphaseDryContactsInventorySensor(coord).extra_state_attributes

    assert attrs["dry_contact_settings_supported"] is True
    assert attrs["dry_contact_settings_contact_count"] == 2
    assert attrs["unmatched_settings"][0]["configured_name"] == "Unmatched Contact"
    assert attrs["contacts"][0]["configured_name"] == "Solar Diverter"
    assert attrs["contacts"][0]["override_supported"] is True
    assert attrs["contacts"][0]["override_active"] is False
    assert attrs["contacts"][0]["control_mode"] == "schedule"
    assert attrs["contacts"][0]["polling_interval_seconds"] == 30
    assert attrs["contacts"][0]["soc_threshold"] == 55
    assert attrs["contacts"][0]["soc_threshold_min"] == 20
    assert attrs["contacts"][0]["soc_threshold_max"] == 80
    assert attrs["contacts"][0]["schedule_windows"] == [
        {"start": "22:00", "end": "06:00"}
    ]
    assert "configured_name" not in attrs["contacts"][1]


def test_dry_contacts_inventory_sensor_single_contact_flattens_settings_attributes(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseDryContactsInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 1,
                "devices": [
                    {
                        "name": "Dry Contact 1",
                        "channel_type": "dry_contact_1",
                        "serial_number": "DC-1",
                        "statusText": "Closed",
                    }
                ],
            },
        },
        ["envoy", "dry_contact"],
    )
    coord.battery_runtime.parse_dry_contact_settings_payload(
        {
            "contacts": [
                {
                    "serial": "DC-1",
                    "displayName": "Solar Diverter",
                    "overrideSupported": True,
                    "controlMode": "soc_threshold",
                    "scheduleWindows": [{"startTime": "21:00", "endTime": "23:00"}],
                }
            ]
        }
    )

    attrs = EnphaseDryContactsInventorySensor(coord).extra_state_attributes

    assert attrs["configured_name"] == "Solar Diverter"
    assert attrs["override_supported"] is True
    assert attrs["control_mode"] == "soc_threshold"
    assert attrs["schedule_windows"] == [{"start": "21:00", "end": "23:00"}]
    assert attrs["contacts"][0]["configured_name"] == "Solar Diverter"


def test_dry_contacts_inventory_sensor_multi_contact_state_is_stable(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseDryContactsInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    members_a = [
        {
            "name": "Dry Contact",
            "channel_type": "dry_contact_2",
            "statusText": "open",
            "serial_number": "SC-1",
        },
        {
            "name": "Dry Contact",
            "channel_type": "dry_contact_1",
            "statusText": "Closed",
            "serial_number": "SC-1",
        },
    ]
    members_b = list(reversed(members_a))

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 2,
                "devices": members_a,
            },
        },
        ["envoy", "dry_contact"],
    )
    sensor = EnphaseDryContactsInventorySensor(coord)
    value_a = sensor.native_value
    attrs_a = sensor.extra_state_attributes
    assert value_a == "Closed | open"
    assert attrs_a["member_count"] == 2
    assert attrs_a["contacts"][0]["channel_type"] == "dry_contact_1"
    assert attrs_a["contacts"][1]["channel_type"] == "dry_contact_2"

    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 2,
                "devices": members_b,
            },
        },
        ["envoy", "dry_contact"],
    )
    assert sensor.native_value == value_a


def test_dry_contacts_inventory_sensor_counts_visible_enabled_in_use(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseDryContactsInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 4,
                "devices": [
                    {
                        "channel_type": "dry_contact_1",
                        "statusText": "Closed",
                        "visible": True,
                        "enabled": True,
                        "in_use": True,
                    },
                    {
                        "channel_type": "dry_contact_2",
                        "statusText": "Open",
                        "visible": False,
                        "enabled": True,
                        "in_use": False,
                    },
                    {
                        "channel_type": "dry_contact_3",
                        "statusText": "Open",
                        "isVisible": True,
                        "isEnabled": False,
                        "active": False,
                    },
                    {"channel_type": "dry_contact_4", "statusText": "Unknown"},
                ],
            },
        },
        ["envoy", "dry_contact"],
    )
    sensor = EnphaseDryContactsInventorySensor(coord)
    attrs = sensor.extra_state_attributes
    assert attrs["member_count"] == 4
    assert attrs["visible_contact_count"] == 2
    assert attrs["enabled_contact_count"] == 2
    assert attrs["in_use_contact_count"] == 1


def test_dry_contacts_inventory_sensor_unavailable_when_super_unavailable(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseDryContactsInventorySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.last_success_utc = None
    coord.last_update_success = False
    coord.has_type_for_entities = lambda _type_key: False  # type: ignore[assignment]
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 1,
                "devices": [{"name": "Dry Contact 1", "statusText": "Closed"}],
            }
        },
        ["dry_contact"],
    )
    assert EnphaseDryContactsInventorySensor(coord).available is False


def test_gateway_helpers_cover_edge_paths(coordinator_factory) -> None:
    class BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    class BadFloat(float):
        def __float__(self) -> float:
            raise ValueError("boom")

    class BadInt:
        def __int__(self) -> int:
            raise ValueError("boom")

    assert sensor_mod._gateway_clean_text(BadStr()) is None
    assert sensor_mod._gateway_optional_bool(True) is True
    assert sensor_mod._gateway_optional_bool(0) is False
    assert sensor_mod._gateway_optional_bool("enable") is True
    assert sensor_mod._gateway_optional_bool("disable") is False
    assert sensor_mod._gateway_optional_bool("maybe") is None
    assert sensor_mod._gateway_channel_type_kind("production_meter") == "production"
    assert sensor_mod._gateway_channel_type_kind("consumption meter") == "consumption"
    assert sensor_mod._gateway_channel_type_kind("unknown") is None
    assert sensor_mod._gateway_channel_type_kind(BadStr()) is None
    assert sensor_mod._is_dry_contact_type_key("dry-contact") is True
    assert sensor_mod._is_dry_contact_type_key("drycontacts") is True
    assert sensor_mod._is_dry_contact_type_key(None) is False
    assert sensor_mod._is_dry_contact_type_key("envoy") is False
    assert sensor_mod._gateway_member_is_dry_contact("bad") is False
    assert (
        sensor_mod._gateway_member_is_dry_contact({"channel_type": "dry_contact_1"})
        is True
    )
    assert sensor_mod._gateway_member_is_dry_contact({"name": "Dry Contact 1"}) is True
    assert (
        sensor_mod._gateway_member_is_dry_contact({"name": "System Controller"})
        is False
    )
    assert sensor_mod._gateway_attr_key("statusText") == "status_text"
    assert sensor_mod._gateway_attr_key("Last-Report") == "last_report"
    assert sensor_mod._gateway_attr_key(BadStr()) is None
    assert sensor_mod._gateway_flat_member_attributes(
        {
            "statusText": "Normal",
            "empty": " ",
            "count": 1,
            "nested": {"bad": True},
            "nullable": None,
        },
        skip_keys={"count"},
    ) == {"status_text": "Normal"}
    assert sensor_mod._gateway_normalize_status(None) == "unknown"
    assert sensor_mod._gateway_normalize_status("critical fault") == "error"
    assert sensor_mod._gateway_normalize_status("mystery") == "unknown"

    aware = datetime(2026, 2, 15, 10, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 2, 15, 10, 0)
    assert sensor_mod._gateway_parse_timestamp(aware) == aware
    assert sensor_mod._gateway_parse_timestamp(naive) == aware
    assert sensor_mod._gateway_parse_timestamp(BadFloat(1.0)) is None
    assert sensor_mod._gateway_parse_timestamp(" ") is None
    assert sensor_mod._gateway_parse_timestamp("not-a-timestamp") is None
    parsed_naive = sensor_mod._gateway_parse_timestamp("2026-02-15T10:00:00")
    assert parsed_naive is not None
    assert parsed_naive.tzinfo is not None
    assert sensor_mod._gateway_parse_timestamp({}) is None
    assert sensor_mod._gateway_parse_timestamp(float("inf")) is None
    assert sensor_mod._battery_parse_timestamp(0) is None
    assert sensor_mod._battery_parse_timestamp("1e20") is None
    assert sensor_mod._battery_parse_timestamp(" ") is None
    assert sensor_mod._battery_parse_timestamp("not-a-timestamp") is None
    assert sensor_mod._battery_parse_timestamp("2026-02-15T08:31:33") == datetime(
        2026, 2, 15, 8, 31, 33, tzinfo=timezone.utc
    )
    parsed_epoch = sensor_mod._battery_parse_timestamp(1_771_144_293_000)
    assert parsed_epoch == datetime(2026, 2, 15, 8, 31, 33, tzinfo=timezone.utc)
    parsed_battery_naive = sensor_mod._battery_parse_timestamp(
        datetime(2026, 2, 15, 8, 31, 33)
    )
    assert parsed_battery_naive == datetime(2026, 2, 15, 8, 31, 33, tzinfo=timezone.utc)
    assert sensor_mod._battery_optional_bool(True) is True
    assert sensor_mod._battery_optional_bool(1) is True
    assert sensor_mod._battery_optional_bool("disabled") is False
    assert sensor_mod._battery_optional_bool("maybe") is None
    assert sensor_mod._battery_last_reported_members(
        SimpleNamespace(
            battery_status_payload=None,
            iter_battery_serials=lambda: ["BAT-3"],
        )
    ) == [{"serial_number": "BAT-3"}]
    snapshot_from_storage = battery_last_reported_snapshot(
        SimpleNamespace(
            battery_status_payload=None,
            iter_battery_serials=lambda: ["BAT-1", "BAT-2"],
            battery_storage=lambda serial: (
                {
                    "serial_number": serial,
                    "name": f"Battery {serial[-1]}",
                    "last_reported_at": "2026-02-15T08:31:33Z",
                }
                if serial == "BAT-1"
                else "bad"
            ),
        )
    )
    assert snapshot_from_storage["total_batteries"] == 2
    assert snapshot_from_storage["without_last_report_count"] == 1
    assert snapshot_from_storage["latest_reported_device"] == {
        "serial_number": "BAT-1",
        "name": "Battery 1",
        "status": None,
    }
    assert sensor_mod._EnphaseBatteryStorageBaseSensor._parse_timestamp(
        "2026-02-15T08:31:33Z"
    ) == datetime(2026, 2, 15, 8, 31, 33, tzinfo=timezone.utc)
    members = sensor_mod._battery_last_reported_members(
        SimpleNamespace(
            battery_status_payload={
                "storages": [
                    "bad",
                    {"serial_number": "BAT-X", "excluded": "true"},
                    {"serial_number": "BAT-1", "last_report": 1_771_144_293},
                ]
            }
        )
    )
    assert members == [{"serial_number": "BAT-1", "last_report": 1_771_144_293}]

    assert sensor_mod._gateway_format_counts({"": 2, "A": BadInt(), "B": 0}) is None
    assert sensor_mod._gateway_format_counts({"B": 2, "A": 2}) == "A x2, B x2"

    assert sensor_mod._gateway_meter_status_text(None) is None
    assert sensor_mod._gateway_meter_status_text({"statusText": "Normal"}) == "Normal"
    assert (
        sensor_mod._gateway_meter_status_text({"status": "NOT_REPORTING"})
        == "Not Reporting"
    )
    assert sensor_mod._gateway_meter_status_text({"status": " "}) is None
    assert sensor_mod._gateway_meter_last_reported(None) is None
    assert sensor_mod._gateway_meter_last_reported({"last_report": "invalid"}) is None
    parsed_report = sensor_mod._gateway_meter_last_reported(
        {"last_reported": "2026-02-15T10:00:00Z"}
    )
    assert parsed_report is not None
    assert sensor_mod._title_case_status("not_reporting") == "Not Reporting"
    assert sensor_mod._title_case_status("_") is None
    assert sensor_mod._gateway_iq_energy_router_inventory_buckets("bad") == []
    assert sensor_mod._gateway_iq_energy_router_inventory_buckets(
        [{"type": "hemsDevices"}]
    ) == [{"type": "hemsDevices"}]
    assert sensor_mod._gateway_iq_energy_router_inventory_buckets(
        {"value": {"result": [{"type": "hemsDevices"}]}}
    ) == [{"type": "hemsDevices"}]
    assert sensor_mod._gateway_iq_energy_router_identity(
        "5956621_IQ_ENERGY_ROUTER_1"
    ) == ("5956621_iq_energy_router_1")
    assert sensor_mod._gateway_iq_energy_router_identity(BadStr()) is None
    assert (
        sensor_mod._gateway_iq_energy_router_member_key(
            {"device-uid": "5956621_IQ_ENERGY_ROUTER_1"},
            fallback_index=1,
        )
        == "5956621_iq_energy_router_1"
    )
    assert (
        sensor_mod._gateway_iq_energy_router_member_key(
            {"uid": "LGX-025"},
            fallback_index=2,
        )
        == "lgx_025"
    )
    assert (
        sensor_mod._gateway_iq_energy_router_member_key(
            {"name": "Router Main"},
            fallback_index=3,
        )
        == "name_router_main"
    )
    assert (
        sensor_mod._gateway_iq_energy_router_member_key({}, fallback_index=4)
        == "index_4"
    )
    assert sensor_mod._gateway_iq_energy_router_last_reported(None) is None
    parsed_router_report = sensor_mod._gateway_iq_energy_router_last_reported(
        {"last-report": "2026-02-15T10:00:00Z"}
    )
    assert parsed_router_report is not None

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inventory_view.type_bucket = lambda _key: {  # type: ignore[assignment]
        "count": BadInt(),
        "devices": [
            {"status": "normal", "connected": None, "model": "IQ Gateway"},
            {"statusText": "not reporting", "connected": None, "sw_version": "8.2.0"},
            {"status": "mystery", "connected": "maybe"},
        ],
    }
    snapshot = sensor_mod._gateway_inventory_snapshot(coord)
    assert snapshot["total_devices"] == 3
    assert snapshot["connected_devices"] == 1
    assert snapshot["disconnected_devices"] == 1
    assert snapshot["unknown_connection_devices"] == 1
    assert snapshot["model_summary"] == "IQ Gateway x1"
    assert snapshot["firmware_summary"] == "8.2.0 x1"
    meter_member = sensor_mod._gateway_meter_member(coord, "production")
    assert meter_member is None

    coord.inventory_view.type_bucket = lambda _key: {  # type: ignore[assignment]
        "count": 2,
        "devices": [
            {"channel_type": "production_meter", "name": "Production Meter"},
            {"name": "Consumption Meter"},
        ],
    }
    assert sensor_mod._gateway_meter_member(coord, "production") is not None
    assert sensor_mod._gateway_meter_member(coord, "consumption") is not None
    coord.inventory_view.type_bucket = lambda _key: {"devices": "bad"}  # type: ignore[assignment]
    assert sensor_mod._gateway_meter_member(coord, "production") is None
    coord.inventory_view.type_bucket = lambda _key: {"devices": ["bad"]}  # type: ignore[assignment]
    assert sensor_mod._gateway_meter_member(coord, "production") is None
    coord.inventory_view.type_bucket = lambda _key: {"count": 0, "devices": []}  # type: ignore[assignment]
    coord.system_dashboard_envoy_detail = lambda: {  # type: ignore[attr-defined]
        "status": "normal",
        "last_report": "2026-03-09T05:45:00+00:00",
    }
    coord._gateway_inventory_summary_cache = {}  # noqa: SLF001
    coord._gateway_inventory_summary_source = None  # noqa: SLF001
    fallback_snapshot = sensor_mod._gateway_inventory_snapshot(coord)
    assert fallback_snapshot["total_devices"] == 1
    assert fallback_snapshot["connected_devices"] == 1
    assert fallback_snapshot["latest_reported_utc"] == "2026-03-09T05:45:00+00:00"
    coord.inventory_view.type_bucket = lambda _key: {  # type: ignore[assignment]
        "devices": [{"channel_type": "production_meter", "name": "Production Meter"}]
    }
    coord.system_dashboard_meter_detail = lambda _kind: {  # type: ignore[attr-defined]
        "meter_state": None,
        "config_type": "Production",
    }
    assert sensor_mod._gateway_meter_member(coord, "production") == {
        "channel_type": "production_meter",
        "name": "Production Meter",
        "config_type": "Production",
    }
    coord.inventory_view.type_bucket = lambda _key: {"devices": "bad"}  # type: ignore[assignment]
    assert sensor_mod._gateway_system_controller_member(coord) is None
    coord.inventory_view.type_bucket = lambda _key: {"devices": ["bad"]}  # type: ignore[assignment]
    assert sensor_mod._gateway_system_controller_member(coord) is None
    coord.inventory_view.type_bucket = lambda _key: {  # type: ignore[assignment]
        "devices": [{"name": "System Controller (Main)"}]
    }
    assert sensor_mod._gateway_system_controller_member(coord) is not None
    coord.inventory_view.type_bucket = lambda _key: {  # type: ignore[assignment]
        "devices": [
            {"name": "Dry Contact Gateway", "channel_type": "dry_contact_1"},
            {"name": "Dry Contact Gateway", "channel_type": "dry_contact_1"},
            "bad-member",
        ]
    }
    coord._type_device_buckets = {  # noqa: SLF001
        "dry_contact": {
            "devices": [
                {"name": "Dry Contact Type", "statusText": "Closed"},
                {"name": "Retired Dry", "statusText": "Retired"},
                "bad",
            ]
        },
        "dry_contact_invalid_bucket": "bad",
        "dry_contact_invalid_members": {"devices": "bad"},
    }
    dry_members = sensor_mod._gateway_dry_contact_members(coord)
    assert len(dry_members) == 2
    assert any(member.get("name") == "Dry Contact Gateway" for member in dry_members)
    assert any(member.get("name") == "Dry Contact Type" for member in dry_members)
    coord.inventory_view.type_bucket = lambda _key: {  # type: ignore[assignment]
        "devices": [
            {
                "name": "Dry Contact",
                "channel_type": "dry_contact_1",
                "serial_number": "SC-1",
            },
            {
                "name": "Dry Contact",
                "channel_type": "dry_contact_2",
                "serial_number": "SC-1",
            },
        ]
    }
    coord._type_device_buckets = {}  # noqa: SLF001
    dry_members = sensor_mod._gateway_dry_contact_members(coord)
    assert len(dry_members) == 2
    coord.inventory_view.type_bucket = lambda _key: {"devices": []}  # type: ignore[assignment]
    coord._type_device_buckets = {  # noqa: SLF001
        "dry_contact": {
            "devices": [
                {"": "ignored", "none": None, "nested": {"bad": True}, "blank": "   "}
            ]
        }
    }
    dry_members = sensor_mod._gateway_dry_contact_members(coord)
    assert len(dry_members) == 1
    coord.inventory_view.type_bucket = lambda _key: {  # type: ignore[assignment]
        "devices": [
            {"device_uid": "DU-1", "channel_type": "dry_contact_1"},
            {"device_uid": "DU-2", "name": "Dry Contact Device UID Only"},
            {"uid": "UID-1", "channel_type": "dry_contact_2"},
            {"uid": "UID-2", "name": "Dry Contact UID Only"},
            {"contact_id": "CID-1", "channel_type": "dry_contact_3"},
            {
                "contact_id": "CID-2",
                "serial_number": "SC-1",
                "name": "Dry Contact Contact+Serial",
            },
            {"contact_id": "CID-3", "name": "Dry Contact Contact Only"},
            {"serial_number": "SC-2", "name": "Dry Contact Serial Only"},
        ]
    }
    coord._type_device_buckets = {}  # noqa: SLF001
    dry_members = sensor_mod._gateway_dry_contact_members(coord)
    assert len(dry_members) == 8
    coord._hems_group_members = None  # type: ignore[assignment]  # noqa: SLF001
    coord._devices_inventory_payload = {  # noqa: SLF001
        "result": [
            {
                "type": "hemsDevices",
                "devices": [
                    {
                        "gateway": [
                            {
                                "device-type": "IQ_ENERGY_ROUTER",
                                "device-uid": "5956621_IQ_ENERGY_ROUTER_1",
                                "last-report": "2026-02-15T10:00:00Z",
                            },
                            {
                                "device-type": "IQ_ENERGY_ROUTER",
                                "device-uid": "5956621_IQ_ENERGY_ROUTER_RETIRED",
                                "statusText": "retired",
                            },
                            {
                                "device-type": "IQ_ENERGY_ROUTER",
                                "uid": "LGX-RETIRED",
                                "isRetired": True,
                            },
                            {
                                "device-type": "IQ_ENERGY_ROUTER",
                                "device-uid": "5956621_IQ_ENERGY_ROUTER_1",
                            },
                            {"device-type": "IQ_GATEWAY"},
                        ]
                    }
                ],
            }
        ]
    }
    router_records = sensor_mod._gateway_iq_energy_router_records(coord)
    assert len(router_records) == 1
    assert router_records[0]["key"] == "5956621_iq_energy_router_1"
    assert (
        sensor_mod._gateway_iq_energy_router_record(
            coord,
            "5956621_iq_energy_router_1",
        )
        is not None
    )
    assert sensor_mod._gateway_iq_energy_router_record(coord, "unknown") is None
    coord._devices_inventory_payload = {  # noqa: SLF001
        "result": [
            {
                "type": "hems_devices",
                "devices": [
                    {
                        "gateway": [
                            {
                                "device-type": "IQ_ENERGY_ROUTER",
                                "device-uid": "5956621_IQ_ENERGY_ROUTER_ALT",
                            }
                        ]
                    }
                ],
            }
        ]
    }
    variant_records = sensor_mod._gateway_iq_energy_router_records(coord)
    assert len(variant_records) == 1
    assert variant_records[0]["key"] == "5956621_iq_energy_router_alt"

    assert (
        sensor_mod._gateway_connectivity_state(
            {
                "total_devices": 2,
                "connected_devices": 2,
                "disconnected_devices": 0,
                "unknown_connection_devices": 0,
            }
        )
        == "online"
    )
    assert (
        sensor_mod._gateway_connectivity_state(
            {
                "total_devices": 2,
                "connected_devices": 0,
                "disconnected_devices": 1,
                "unknown_connection_devices": 1,
            }
        )
        == "offline"
    )
    assert (
        sensor_mod._gateway_connectivity_state(
            {
                "total_devices": 2,
                "connected_devices": 0,
                "disconnected_devices": 0,
                "unknown_connection_devices": 2,
            }
        )
        == "unknown"
    )
    assert (
        sensor_mod._gateway_connectivity_state(
            {
                "total_devices": 2,
                "connected_devices": 0,
                "disconnected_devices": 0,
                "unknown_connection_devices": 1,
            }
        )
        == "degraded"
    )


def test_gateway_inventory_snapshot_fallback_covers_summary_error_and_dashboard_fallback() -> (
    None
):
    coord = SimpleNamespace(
        gateway_inventory_summary=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        type_bucket=lambda _type_key: {
            "count": "bad-count",
            "devices": [
                {
                    "name": "Gateway A",
                    "serial_number": "GW-A",
                    "statusText": "Normal",
                    "model": "IQ Gateway",
                    "sw_version": "8.2.0",
                    "last_reported": "2026-02-15T10:00:00Z",
                },
                {
                    "name": "Gateway B",
                    "serial_number": "GW-B",
                    "status": "offline",
                    "connected": None,
                    "model_name": "System Controller",
                    "firmware": "1.0.0",
                },
                {
                    "name": "Gateway C",
                    "serial_number": "GW-C",
                    "status": "warn",
                    "connected": "maybe",
                    "firmwareVersion": "2.0.0",
                    "last-report": "2026-02-15T10:05:00Z",
                },
            ],
        },
        system_dashboard_envoy_detail=lambda: {
            "name": "",
            "serial_number": "GW-D",
            "status": "normal",
            "last_interval_end_date": "2026-02-15T10:10:00Z",
        },
    )

    snapshot = sensor_mod._gateway_inventory_snapshot(coord)
    assert snapshot["total_devices"] == 3
    assert snapshot["connected_devices"] == 1
    assert snapshot["disconnected_devices"] == 1
    assert snapshot["unknown_connection_devices"] == 1
    assert snapshot["without_last_report_count"] == 2
    assert snapshot["status_counts"] == {
        "normal": 1,
        "warning": 1,
        "error": 0,
        "not_reporting": 1,
        "unknown": 0,
    }
    assert snapshot["status_summary"] == (
        "Normal 1 | Warning 1 | Error 0 | Not Reporting 1 | Unknown 0"
    )
    assert snapshot["model_summary"] == "IQ Gateway x1"
    assert snapshot["firmware_summary"] == "8.2.0 x1"
    assert snapshot["latest_reported_device"] == {
        "name": "Gateway A",
        "serial_number": "GW-A",
        "status": "Normal",
    }
    assert "statusText" in snapshot["property_keys"]

    dashboard_only_coord = SimpleNamespace(
        gateway_inventory_summary=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        type_bucket=lambda _type_key: {"count": 0, "devices": []},
        system_dashboard_envoy_detail=lambda: {
            "name": "",
            "serial_number": "GW-FALLBACK",
            "statusText": "Normal",
            "last_interval_end_date": "2026-02-15T10:20:00Z",
        },
    )
    fallback_snapshot = sensor_mod._gateway_inventory_snapshot(dashboard_only_coord)
    assert fallback_snapshot["total_devices"] == 1
    assert fallback_snapshot["latest_reported_device"] == {
        "name": "IQ Gateway",
        "serial_number": "GW-FALLBACK",
        "status": "Normal",
    }
    assert fallback_snapshot["latest_reported_utc"] == "2026-02-15T10:20:00+00:00"


def test_gateway_inventory_snapshot_zero_devices_returns_empty_status_summary() -> None:
    coord = SimpleNamespace(
        gateway_inventory_summary=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        type_bucket=lambda _type_key: {"count": 0, "devices": []},
        system_dashboard_envoy_detail=lambda: None,
    )

    snapshot = sensor_mod._gateway_inventory_snapshot(coord)
    assert snapshot["total_devices"] == 0
    assert snapshot["status_summary"] is None
    assert snapshot["latest_reported_device"] is None


def test_microinverter_snapshot_fallback_covers_bad_counts_and_missing_status_counts() -> (
    None
):
    class BadInt:
        def __int__(self) -> int:
            raise ValueError("bad")

    coord = SimpleNamespace(
        microinverter_inventory_summary=lambda: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
        type_bucket=lambda _type_key: {
            "count": BadInt(),
            "devices": [
                {"serial_number": "INV-A", "last_report": "invalid"},
                {
                    "serial_number": "INV-B",
                    "name": "Inverter B",
                    "status": "warning",
                    "last_reported_at": "2026-02-15T11:00:00Z",
                },
            ],
            "status_counts": {
                "total": 1,
                "normal": BadInt(),
                "warning": 0,
                "error": 0,
                "not_reporting": 1,
                "unknown": 2,
            },
            "latest_reported_utc": "2026-02-15T07:00:00Z",
            "latest_reported_device": {
                "serial_number": "TOPOLOGY",
                "name": "Topology summary",
                "status": "normal",
            },
            "connectivity_state": " ",
        },
    )

    snapshot = sensor_mod._microinverter_inventory_snapshot(coord)
    assert snapshot["total_inverters"] == 2
    assert snapshot["not_reporting_inverters"] == 1
    assert snapshot["unknown_inverters"] == 1
    assert snapshot["latest_reported_device"] == {
        "serial_number": "INV-B",
        "name": "Inverter B",
        "status": "warning",
    }
    assert snapshot["latest_reported_utc"] == "2026-02-15T11:00:00+00:00"
    assert snapshot["connectivity_state"] == "offline"

    missing_status_counts_coord = SimpleNamespace(
        microinverter_inventory_summary=lambda: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
        type_bucket=lambda _type_key: {
            "count": 2,
            "devices": [],
            "status_counts": "bad",
            "connectivity_state": " ",
        },
    )
    snapshot2 = sensor_mod._microinverter_inventory_snapshot(
        missing_status_counts_coord
    )
    assert snapshot2["unknown_inverters"] == 2
    assert snapshot2["connectivity_state"] == "unknown"
    assert (
        sensor_mod._microinverter_connectivity_state(
            {
                "total_inverters": 3,
                "reporting_inverters": 1,
                "not_reporting_inverters": 0,
                "unknown_inverters": 0,
            }
        )
        == "degraded"
    )


def test_gateway_diagnostic_sensor_availability_paths(coordinator_factory) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConnectivityStatusSensor,
        EnphaseGatewayLastReportedSensor,
    )

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {
        "envoy": {"count": 1, "devices": [{"name": "GW"}]}
    }  # noqa: SLF001
    coord.last_update_success = False
    coord.last_success_utc = None

    assert EnphaseGatewayConnectivityStatusSensor(coord).available is False
    assert EnphaseGatewayLastReportedSensor(coord).available is False

    coord.last_update_success = True
    coord._devices_inventory_ready = False  # noqa: SLF001
    coord._type_device_buckets = {"envoy": {"count": 0, "devices": []}}  # noqa: SLF001
    assert EnphaseGatewayConnectivityStatusSensor(coord).available is True

    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._type_device_buckets = {
        "envoy": {"count": 1, "devices": [{"name": "GW"}]}
    }  # noqa: SLF001
    assert EnphaseGatewayLastReportedSensor(coord).available is False

    coord._type_device_buckets = {  # noqa: SLF001
        "envoy": {
            "count": 1,
            "devices": [
                {
                    "name": "GW",
                    "status": "normal",
                    "last_report": "2026-02-15T10:00:00Z",
                }
            ],
        }
    }
    assert EnphaseGatewayConnectivityStatusSensor(coord).available is True
    assert EnphaseGatewayLastReportedSensor(coord).available is True


def test_inverter_lifetime_sensor_snapshot_handles_non_callable_getter(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseInverterLifetimeEnergySensor

    coord = coordinator_factory(serials=[RANDOM_SERIAL])
    coord.inverter_data = None  # type: ignore[assignment]
    entity = EnphaseInverterLifetimeEnergySensor(coord, "INV-A")
    assert entity.native_value is None


@pytest.mark.asyncio
async def test_async_setup_entry_adds_site_energy_entities(
    hass, config_entry, coordinator_factory, monkeypatch
):
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.energy.site_energy = {
        "grid_import": SimpleNamespace(
            value_kwh=1.0,
            bucket_count=1,
            fields_used=["import"],
            start_date="2024-01-01",
            last_report_date=None,
            update_pending=False,
            source_unit="Wh",
        )
    }

    callbacks: list = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_topology_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list = []

    def _async_add_entities(entities, update_before_add=False):
        added.extend(entities)

    created: list = []
    created_power: list = []
    created_battery_power: list = []

    class StubSiteEnergy(sensor_mod.EnphaseSiteEnergySensor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    class StubGridPower(sensor_mod.EnphaseGridPowerSensor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_power.append(self)

    class StubBatteryPower(sensor_mod.EnphaseBatteryPowerSensor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_battery_power.append(self)

    monkeypatch.setattr(sensor_mod, "EnphaseSiteEnergySensor", StubSiteEnergy)
    monkeypatch.setattr(sensor_mod, "EnphaseGridPowerSensor", StubGridPower)
    monkeypatch.setattr(sensor_mod, "EnphaseBatteryPowerSensor", StubBatteryPower)

    await async_setup_entry(hass, config_entry, _async_add_entities)
    for cb in callbacks:
        cb()
    assert created, "Expected site energy sensor to be created"
    assert created_power, "Expected grid power sensor to be created"
    assert any(ent._flow_key == "consumption" for ent in created)
    assert any(ent.translation_key == "site_consumption" for ent in created)
    assert any(ent._flow_key == "evse_charging" for ent in created)
    assert any(ent.translation_key == "site_evse_charging" for ent in created)
    assert not any(ent._flow_key == "heat_pump" for ent in created)
    assert not any(
        ent.translation_key == "site_heat_pump_consumption" for ent in created
    )
    assert not any(ent._flow_key == "water_heater" for ent in created)
    assert not any(
        ent.translation_key == "site_water_heater_consumption" for ent in created
    )
    assert created_power[0].translation_key == "site_grid_power"
    assert not created_battery_power


@pytest.mark.asyncio
async def test_async_setup_entry_adds_optional_site_power_entities_when_supported(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord._battery_has_encharge = True  # noqa: SLF001
    coord._battery_storage_data = {  # noqa: SLF001
        "BAT-1": {"identity": "BAT-1", "serial_number": "BAT-1"}
    }
    coord._battery_storage_order = ["BAT-1"]  # noqa: SLF001
    coord.energy._site_energy_meta = {  # noqa: SLF001
        "bucket_lengths": {
            "import": "supported",
            "solar_grid": "supported",
            "charge": "supported",
            "discharge": "supported",
        }
    }

    callbacks: list = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_topology_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    created_power: list = []
    created_battery_power: list = []

    class StubGridPower(sensor_mod.EnphaseGridPowerSensor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_power.append(self)

    class StubBatteryPower(sensor_mod.EnphaseBatteryPowerSensor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_battery_power.append(self)

    monkeypatch.setattr(sensor_mod, "EnphaseGridPowerSensor", StubGridPower)
    monkeypatch.setattr(sensor_mod, "EnphaseBatteryPowerSensor", StubBatteryPower)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)
    for cb in callbacks:
        cb()

    assert created_power[0].translation_key == "site_grid_power"
    assert created_battery_power[0].translation_key == "site_battery_power"


@pytest.mark.asyncio
async def test_async_setup_entry_adds_optional_site_energy_entities_when_supported(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"device_uid": "HP-1", "device_type": "HEAT_PUMP"}],
            }
        },
        ["heatpump"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.energy._site_energy_meta = {
        "bucket_lengths": {"water_heater": 1}
    }  # noqa: SLF001

    callbacks: list = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_topology_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    created: list = []

    class StubSiteEnergy(sensor_mod.EnphaseSiteEnergySensor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(sensor_mod, "EnphaseSiteEnergySensor", StubSiteEnergy)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)
    for cb in callbacks:
        cb()

    assert any(ent._flow_key == "heat_pump" for ent in created)
    assert any(ent.translation_key == "site_heat_pump_consumption" for ent in created)
    assert any(ent._flow_key == "water_heater" for ent in created)
    assert any(
        ent.translation_key == "site_water_heater_consumption" for ent in created
    )


@pytest.mark.asyncio
async def test_async_setup_entry_skips_water_heater_site_energy_without_device_channel(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.energy._site_energy_meta = {
        "bucket_lengths": {"water_heater": 0}
    }  # noqa: SLF001

    callbacks: list = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_topology_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    created: list = []

    class StubSiteEnergy(sensor_mod.EnphaseSiteEnergySensor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(sensor_mod, "EnphaseSiteEnergySensor", StubSiteEnergy)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)
    for cb in callbacks:
        cb()

    assert not any(ent._flow_key == "water_heater" for ent in created)
    assert not any(
        ent.translation_key == "site_water_heater_consumption" for ent in created
    )


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_water_heater_site_energy_when_flow_exists(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.energy.site_energy = {
        "water_heater": {
            "value_kwh": 1.25,
            "bucket_count": 1,
            "fields_used": ["water_heater"],
            "start_date": "2026-03-10",
            "last_report_date": None,
            "source_unit": "Wh",
        }
    }
    coord.energy._site_energy_meta = {
        "bucket_lengths": {"water_heater": 0}
    }  # noqa: SLF001

    callbacks: list = []

    def fake_add_listener(cb):
        callbacks.append(cb)
        return lambda: None

    coord.async_add_topology_listener = fake_add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    created: list = []

    class StubSiteEnergy(sensor_mod.EnphaseSiteEnergySensor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(sensor_mod, "EnphaseSiteEnergySensor", StubSiteEnergy)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)
    for cb in callbacks:
        cb()

    assert any(ent._flow_key == "water_heater" for ent in created)
    assert any(
        ent.translation_key == "site_water_heater_consumption" for ent in created
    )


def test_site_heat_pump_energy_sensor_uses_heatpump_device_info(
    coordinator_factory,
) -> None:
    from homeassistant.helpers.entity import DeviceInfo

    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import EnphaseSiteEnergySensor

    coord = coordinator_factory(serials=[])
    expected = DeviceInfo(
        identifiers={(DOMAIN, f"type:{coord.site_id}:heatpump")},
        manufacturer="Enphase",
        name="Heat Pump",
    )
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"device_uid": "HP-1", "device_type": "HEAT_PUMP"}],
            }
        },
        ["heatpump"],
    )
    coord.inventory_view.type_device_info = lambda key: expected if key == "heatpump" else None  # type: ignore[assignment]
    coord._heatpump_power_w = 725.125  # noqa: SLF001
    coord._heatpump_daily_consumption = {  # noqa: SLF001
        "daily_energy_wh": 230.0,
        "split_daily_energy_wh": 230.0,
        "daily_solar_wh": 10.0,
        "daily_battery_wh": 20.0,
        "daily_grid_wh": 200.0,
        "device_uid": None,
        "device_name": None,
        "split_device_uid": "HP-1",
        "split_device_name": "Heat Pump",
        "member_name": "Primary Heat Pump",
        "member_device_type": "HEAT_PUMP",
        "pairing_status": "PAIRED",
        "device_state": "ACTIVE",
        "endpoint_type": "site-today",
        "endpoint_timestamp": "2026-03-20T08:00:00Z",
        "split_endpoint_type": "hems-device-details",
        "split_endpoint_timestamp": "2026-03-20T07:53:00.739143826Z",
        "day_key": "2026-03-20",
        "timezone": "Europe/Berlin",
        "source": "site_today_heatpump",
        "split_source": "hems_energy_consumption:HP-1",
    }
    coord.energy.site_energy = {
        "heat_pump": {
            "value_kwh": 2.0,
            "bucket_count": 1,
            "fields_used": ["heatpump"],
            "start_date": "2026-02-27",
            "last_report_date": None,
            "source_unit": "Wh",
        }
    }

    sensor = EnphaseSiteEnergySensor(
        coord,
        "heat_pump",
        "site_heat_pump_consumption",
        "Site Heat Pump Consumption",
    )
    assert sensor.device_info is expected
    attrs = sensor.extra_state_attributes
    assert attrs["heat_pump_power_w"] == pytest.approx(725.125)
    assert attrs["daily_energy_wh"] == pytest.approx(230.0)
    assert attrs["daily_grid_wh"] == pytest.approx(200.0)
    assert attrs["daily_device_uid"] is None
    assert attrs["daily_device_name"] is None
    assert attrs["daily_member_name"] == "Primary Heat Pump"
    assert attrs["daily_member_device_type"] == "HEAT_PUMP"
    assert attrs["daily_pairing_status"] == "PAIRED"
    assert attrs["daily_device_state"] == "ACTIVE"
    assert attrs["daily_endpoint_type"] == "site-today"
    assert attrs["daily_endpoint_timestamp"] == "2026-03-20T08:00:00Z"
    assert attrs["day_key"] == "2026-03-20"
    assert attrs["timezone"] == "Europe/Berlin"
    assert attrs["daily_source"] == "site_today_heatpump"


def test_heat_pump_daily_energy_and_sg_ready_gateway_sensors(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseHeatPumpDailyBatteryEnergySensor,
        EnphaseHeatPumpDailyEnergySensor,
        EnphaseHeatPumpDailyGridEnergySensor,
        EnphaseHeatPumpDailySolarEnergySensor,
        EnphaseHeatPumpSgReadyGatewaySensor,
        _heatpump_daily_snapshot,
    )
    from homeassistant.components.sensor import SensorStateClass

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 2,
                "devices": [
                    {
                        "device_uid": "HP-1",
                        "device_type": "HEAT_PUMP",
                        "name": "Heat Pump",
                        "pairing_status": "PAIRED",
                        "device_state": "ACTIVE",
                    },
                    {
                        "device_uid": "HP-SG-1",
                        "device_type": "SG_READY_GATEWAY",
                        "name": "SG Ready Gateway",
                        "statusText": "Normal",
                    },
                ],
            }
        },
        ["heatpump"],
    )
    coord._heatpump_daily_consumption = {  # noqa: SLF001
        "daily_energy_wh": 230.0,
        "split_daily_energy_wh": 230.0,
        "daily_solar_wh": 10.0,
        "daily_battery_wh": 20.0,
        "daily_grid_wh": 200.0,
        "device_uid": None,
        "device_name": None,
        "split_device_uid": "HP-1",
        "split_device_name": "Heat Pump",
        "member_name": "Heat Pump",
        "member_device_type": "HEAT_PUMP",
        "pairing_status": "PAIRED",
        "device_state": "ACTIVE",
        "endpoint_type": "site-today",
        "endpoint_timestamp": "2026-03-20T08:00:00Z",
        "split_endpoint_type": "hems-device-details",
        "split_endpoint_timestamp": "2026-03-20T07:53:00.739143826Z",
        "day_key": "2026-03-20",
        "timezone": "Europe/Berlin",
        "details": [230.0],
        "source": "site_today_heatpump",
        "split_source": "hems_energy_consumption:HP-1",
    }
    coord._heatpump_daily_consumption_last_success_utc = datetime(  # noqa: SLF001
        2026, 3, 20, 8, 0, tzinfo=timezone.utc
    )
    coord._heatpump_daily_split_last_success_utc = datetime(  # noqa: SLF001
        2026, 3, 20, 7, 53, tzinfo=timezone.utc
    )
    coord._heatpump_daily_split_using_stale = True  # noqa: SLF001
    coord._heatpump_daily_split_last_error = (
        "No usable HEMS daily split payload"  # noqa: SLF001
    )

    daily_sensor = EnphaseHeatPumpDailyEnergySensor(coord)
    grid_sensor = EnphaseHeatPumpDailyGridEnergySensor(coord)
    solar_sensor = EnphaseHeatPumpDailySolarEnergySensor(coord)
    battery_sensor = EnphaseHeatPumpDailyBatteryEnergySensor(coord)
    gateway_sensor = EnphaseHeatPumpSgReadyGatewaySensor(coord)

    assert daily_sensor.available is True
    assert daily_sensor.state_class == SensorStateClass.TOTAL
    assert daily_sensor.native_value == pytest.approx(0.23)
    assert daily_sensor.extra_state_attributes["source"] == "site_today_heatpump"
    assert (
        grid_sensor.extra_state_attributes["source"] == "hems_energy_consumption:HP-1"
    )
    assert grid_sensor.extra_state_attributes["using_stale"] is True
    assert (
        grid_sensor.extra_state_attributes["last_success_utc"]
        == "2026-03-20T07:53:00+00:00"
    )
    assert (
        grid_sensor.extra_state_attributes["last_error"]
        == "No usable HEMS daily split payload"
    )
    assert grid_sensor.available is True
    assert grid_sensor.native_value == pytest.approx(0.2)
    assert grid_sensor.entity_registry_enabled_default is False
    assert solar_sensor.native_value == pytest.approx(0.01)
    assert battery_sensor.native_value == pytest.approx(0.02)
    assert gateway_sensor.available is True
    assert gateway_sensor.native_value == "Normal"
    assert gateway_sensor.entity_registry_enabled_default is False

    coord._heatpump_daily_consumption = {"daily_energy_wh": None}  # noqa: SLF001
    assert daily_sensor.native_value is None

    coord._heatpump_daily_consumption = {"daily_energy_wh": "bad"}  # noqa: SLF001
    assert daily_sensor.native_value is None

    coord.last_success_utc = None
    coord.last_update_success = False
    coord._heatpump_daily_consumption = {"daily_energy_wh": 230.0}  # noqa: SLF001
    assert daily_sensor.available is False
    assert (
        _heatpump_daily_snapshot(SimpleNamespace(heatpump_daily_consumption=[])) == {}
    )


def test_site_heat_pump_energy_sensor_ignores_phantom_heatpump_device_info(
    coordinator_factory,
) -> None:
    from homeassistant.helpers.entity import DeviceInfo

    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import EnphaseSiteEnergySensor

    coord = coordinator_factory(serials=[])
    phantom = DeviceInfo(
        identifiers={(DOMAIN, f"type:{coord.site_id}:heatpump")},
        manufacturer="Enphase",
        name="Heat Pump",
    )
    expected_cloud = DeviceInfo(
        identifiers={(DOMAIN, f"cloud:{coord.site_id}")},
        manufacturer="Enphase",
        name="Enphase Cloud",
    )
    coord.inventory_view.type_device_info = lambda key: (  # type: ignore[assignment]
        phantom if key == "heatpump" else expected_cloud if key == "cloud" else None
    )
    coord.energy.site_energy = {
        "heat_pump": {
            "value_kwh": 1.0,
            "bucket_count": 1,
            "fields_used": ["heatpump"],
            "start_date": "2026-02-27",
            "last_report_date": None,
            "source_unit": "Wh",
        }
    }

    sensor = EnphaseSiteEnergySensor(
        coord,
        "heat_pump",
        "site_heat_pump_consumption",
        "Site Heat Pump Consumption",
    )
    assert sensor.device_info is expected_cloud


def test_cloud_sensor_device_info_falls_back_to_default_cloud_device(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseCurrentPowerConsumptionSensor,
        EnphaseCloudLatencySensor,
        EnphaseSiteBackoffEndsSensor,
        EnphaseSiteEnergySensor,
        EnphaseSiteLastErrorCodeSensor,
        EnphaseSiteServiceStatusSensor,
    )

    coord = coordinator_factory(serials=[])
    coord.inventory_view.type_device_info = lambda _key: None  # type: ignore[assignment]
    coord.energy.site_energy = {
        "home": {
            "value_kwh": 1.25,
            "bucket_count": 1,
            "fields_used": ["home"],
            "start_date": "2026-02-27",
            "last_report_date": None,
            "source_unit": "Wh",
        }
    }

    site_energy = EnphaseSiteEnergySensor(
        coord,
        "home",
        "site_home_consumption",
        "Site Home Consumption",
    )
    current_power = EnphaseCurrentPowerConsumptionSensor(coord)
    latency = EnphaseCloudLatencySensor(coord)
    last_error = EnphaseSiteLastErrorCodeSensor(coord)
    service_status = EnphaseSiteServiceStatusSensor(coord)
    backoff = EnphaseSiteBackoffEndsSensor(coord)

    expected_identifiers = {("enphase_ev", f"type:{coord.site_id}:cloud")}
    assert site_energy.device_info["identifiers"] == expected_identifiers
    assert current_power.device_info["identifiers"] == expected_identifiers
    assert latency.device_info["identifiers"] == expected_identifiers
    assert last_error.device_info["identifiers"] == expected_identifiers
    assert service_status.device_info["identifiers"] == expected_identifiers
    assert backoff.device_info["identifiers"] == expected_identifiers


def test_cloud_latency_sensor_device_info_falls_back_without_type_device_info(
    coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseCloudLatencySensor

    coord = coordinator_factory(serials=[])
    monkeypatch.setattr(sensor_mod, "_type_device_info", lambda *_args, **_kwargs: None)

    sensor = EnphaseCloudLatencySensor(coord)

    assert sensor.device_info["identifiers"] == {
        ("enphase_ev", f"type:{coord.site_id}:cloud")
    }


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_gateway_site_entities_when_inventory_unknown(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseCloudLatencySensor,
        EnphaseCurrentPowerConsumptionSensor,
        EnphaseGatewayConsumptionMeterSensor,
        EnphaseMicroinverterConnectivityStatusSensor,
        EnphaseMicroinverterLastReportedSensor,
        EnphaseMicroinverterReportingCountSensor,
        EnphaseGatewayProductionMeterSensor,
        EnphaseSystemControllerInventorySensor,
        EnphaseSiteLastUpdateSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord._type_device_buckets = {}  # noqa: SLF001
    coord._type_device_order = []  # noqa: SLF001
    coord._devices_inventory_ready = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(ent, EnphaseSiteLastUpdateSensor) for ent in added)
    assert any(isinstance(ent, EnphaseCloudLatencySensor) for ent in added)
    assert any(isinstance(ent, EnphaseCurrentPowerConsumptionSensor) for ent in added)
    assert any(isinstance(ent, EnphaseSystemControllerInventorySensor) for ent in added)
    assert any(isinstance(ent, EnphaseGatewayProductionMeterSensor) for ent in added)
    assert any(isinstance(ent, EnphaseGatewayConsumptionMeterSensor) for ent in added)
    assert any(
        isinstance(ent, EnphaseMicroinverterConnectivityStatusSensor) for ent in added
    )
    assert any(
        isinstance(ent, EnphaseMicroinverterReportingCountSensor) for ent in added
    )
    assert any(isinstance(ent, EnphaseMicroinverterLastReportedSensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_cloud_site_entities_without_envoy_type(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseCloudLatencySensor,
        EnphaseCurrentPowerConsumptionSensor,
        EnphaseSiteBackoffEndsSensor,
        EnphaseSiteLastErrorCodeSensor,
        EnphaseSiteServiceStatusSensor,
        EnphaseSiteLastUpdateSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"serial_number": "EV1", "name": "Garage EV"}],
            }
        },
        ["iqevse"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(ent, EnphaseSiteLastUpdateSensor) for ent in added)
    assert any(isinstance(ent, EnphaseCloudLatencySensor) for ent in added)
    assert any(isinstance(ent, EnphaseCurrentPowerConsumptionSensor) for ent in added)
    assert any(isinstance(ent, EnphaseSiteLastErrorCodeSensor) for ent in added)
    assert any(isinstance(ent, EnphaseSiteServiceStatusSensor) for ent in added)
    assert any(isinstance(ent, EnphaseSiteBackoffEndsSensor) for ent in added)


def test_site_service_status_sensor_reports_degraded_services(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseSiteServiceStatusSensor

    coord = coordinator_factory(serials=[])
    coord.collect_site_metrics = lambda: {
        "degraded_services": ["site_energy", "battery_status"],
        "degraded_endpoint_families": ["battery_status"],
        "endpoint_failure_details": {
            "battery_status": {
                "reason": f"Site {coord.site_id} temporarily unavailable",
                "retry_utc": "2026-07-12T01:02:03Z",
            },
            "invalid": "bad",
            None: {"reason": "ignored"},
            "missing_reason": {"retry_utc": "bad"},
        },
    }
    sensor = EnphaseSiteServiceStatusSensor(coord)

    assert sensor.native_value == "degraded"
    assert sensor.icon == "mdi:cloud-alert"
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    attrs = sensor.extra_state_attributes
    assert attrs["degraded_services"] == ["battery_status", "site_energy"]
    assert attrs["degraded_endpoint_families"] == ["battery_status"]
    assert attrs["degraded_service_count"] == 2
    assert attrs["degraded_endpoint_family_count"] == 1
    assert attrs["metrics_available"] is True
    assert attrs["endpoint_failure_details"] == {
        "battery_status": {
            "reason": "Site [site] temporarily unavailable",
            "retry_utc": "2026-07-12T01:02:03+00:00",
        }
    }
    assert "endpoint_failure_details" in sensor._unrecorded_attributes  # noqa: SLF001


def test_site_service_status_sensor_reports_unknown_for_invalid_metrics(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseSiteServiceStatusSensor

    coord = coordinator_factory(serials=[])
    coord.collect_site_metrics = lambda: "bad"
    sensor = EnphaseSiteServiceStatusSensor(coord)

    assert sensor.native_value == "unknown"
    assert sensor.icon == "mdi:cloud-question"
    assert sensor.extra_state_attributes["degraded_services"] == []
    assert sensor.extra_state_attributes["metrics_available"] is False


def test_site_service_status_sensor_reports_ok_without_degraded_services(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseSiteServiceStatusSensor

    coord = coordinator_factory(serials=[])
    coord.collect_site_metrics = lambda: {
        "degraded_services": [],
        "degraded_endpoint_families": None,
        "endpoint_failure_details": {
            "retry_invalid": {"reason": "failed", "retry_utc": "bad"}
        },
    }
    sensor = EnphaseSiteServiceStatusSensor(coord)

    assert sensor.native_value == "ok"
    assert sensor.icon == "mdi:cloud-check"
    assert sensor.extra_state_attributes["degraded_services"] == []
    assert sensor.extra_state_attributes["endpoint_failure_details"] == {
        "retry_invalid": {"reason": "failed", "retry_utc": None}
    }


def test_site_service_status_sensor_handles_metrics_errors(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseSiteServiceStatusSensor

    coord = coordinator_factory(serials=[])

    def _raise_metrics():
        raise RuntimeError("boom")

    coord.collect_site_metrics = _raise_metrics
    sensor = EnphaseSiteServiceStatusSensor(coord)

    assert sensor.native_value == "unknown"
    assert sensor.extra_state_attributes["degraded_endpoint_families"] == []
    assert sensor.extra_state_attributes["metrics_available"] is False


def test_site_service_status_sensor_handles_missing_metrics_callback(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseSiteServiceStatusSensor

    coord = coordinator_factory(serials=[])
    coord.collect_site_metrics = None
    sensor = EnphaseSiteServiceStatusSensor(coord)

    assert sensor.native_value == "unknown"


def test_site_service_status_sensor_reuses_cached_metrics_snapshot(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseSiteServiceStatusSensor

    coord = coordinator_factory(serials=[])
    calls = 0

    def _metrics():
        nonlocal calls
        calls += 1
        return {"degraded_services": ["site_energy"]}

    coord.collect_site_metrics = _metrics
    sensor = EnphaseSiteServiceStatusSensor(coord)

    assert sensor.native_value == "degraded"
    assert sensor.extra_state_attributes["degraded_services"] == ["site_energy"]
    assert calls == 1


def test_site_service_status_sensor_refreshes_snapshot_on_coordinator_update(
    coordinator_factory,
) -> None:
    from custom_components.enphase_ev.sensor import EnphaseSiteServiceStatusSensor

    coord = coordinator_factory(serials=[])
    degraded = False

    def _metrics():
        return {"degraded_services": ["site_energy"] if degraded else []}

    coord.collect_site_metrics = _metrics
    sensor = EnphaseSiteServiceStatusSensor(coord)
    sensor.async_write_ha_state = lambda: None

    assert sensor.native_value == "ok"
    degraded = True
    sensor._handle_coordinator_update()
    assert sensor.native_value == "degraded"


@pytest.mark.asyncio
async def test_async_setup_entry_skips_unsupported_gateway_meter_when_inventory_ready(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConsumptionMeterSensor,
        EnphaseGatewayProductionMeterSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"channel_type": "production_meter", "name": "Production"}],
            }
        },
        ["envoy"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(ent, EnphaseGatewayProductionMeterSensor) for ent in added)
    assert not any(
        isinstance(ent, EnphaseGatewayConsumptionMeterSensor) for ent in added
    )


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_stale_unsupported_gateway_meter_entity(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"channel_type": "production_meter", "name": "Production"}],
            }
        },
        ["envoy"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    stale_unique_id = f"enphase_ev_site_{coord.site_id}_gateway_consumption_meter"
    fake_registry = SimpleNamespace(
        entities={},
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(
            side_effect=lambda domain, platform, unique_id: (
                "sensor.gateway_consumption_meter"
                if domain == "sensor"
                and platform == "enphase_ev"
                and unique_id == stale_unique_id
                else None
            ),
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert fake_registry.async_remove.call_count >= 1
    fake_registry.async_remove.assert_any_call("sensor.gateway_consumption_meter")


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_stale_optional_site_energy_entities(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1"}],
            }
        },
        ["envoy"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    stale_heat_pump_unique_id = f"enphase_ev_site_{coord.site_id}_heat_pump"
    stale_water_heater_unique_id = f"enphase_ev_site_{coord.site_id}_water_heater"
    stale_grid_import_power_unique_id = (
        f"enphase_ev_site_{coord.site_id}_grid_import_power"
    )
    stale_grid_export_power_unique_id = (
        f"enphase_ev_site_{coord.site_id}_grid_export_power"
    )
    stale_battery_power_unique_id = f"enphase_ev_site_{coord.site_id}_battery_power"

    def _fake_get_entity_id(domain, platform, unique_id):
        if domain != "sensor" or platform != "enphase_ev":
            return None
        return {
            stale_heat_pump_unique_id: "sensor.site_heat_pump_consumption",
            stale_water_heater_unique_id: "sensor.site_water_heater_consumption",
            stale_grid_import_power_unique_id: "sensor.grid_import_power",
            stale_grid_export_power_unique_id: "sensor.grid_export_power",
            stale_battery_power_unique_id: "sensor.battery_power",
        }.get(unique_id)

    fake_registry = SimpleNamespace(
        entities={},
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(side_effect=_fake_get_entity_id),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_remove.assert_any_call("sensor.site_heat_pump_consumption")
    fake_registry.async_remove.assert_any_call("sensor.site_water_heater_consumption")
    fake_registry.async_remove.assert_any_call("sensor.grid_import_power")
    fake_registry.async_remove.assert_any_call("sensor.grid_export_power")
    fake_registry.async_remove.assert_any_call("sensor.battery_power")


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_renamed_active_site_energy_entities(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1"}],
            }
        },
        ["envoy"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    renamed_solar_unique_id = f"enphase_ev_site_{coord.site_id}_solar_production"
    renamed_import_unique_id = f"enphase_ev_site_{coord.site_id}_grid_import"

    def _fake_get_entity_id(domain, platform, unique_id):
        if domain != "sensor" or platform != "enphase_ev":
            return None
        return {
            renamed_solar_unique_id: "sensor.envoy_lifetime_production",
            renamed_import_unique_id: "sensor.envoy_lifetime_net_consumption",
        }.get(unique_id)

    fake_registry = SimpleNamespace(
        entities={},
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(side_effect=_fake_get_entity_id),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_remove.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_gateway_meters_when_meter_detection_errors(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseDryContactsInventorySensor,
        EnphaseGatewayConsumptionMeterSensor,
        EnphaseGatewayProductionMeterSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord.inventory_view.has_type_for_entities = lambda type_key: type_key == "envoy"  # type: ignore[assignment]

    def _raise_type_bucket(_type_key):
        raise RuntimeError("boom")

    coord.inventory_view.type_bucket = _raise_type_bucket  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(ent, EnphaseDryContactsInventorySensor) for ent in added)
    assert any(isinstance(ent, EnphaseGatewayProductionMeterSensor) for ent in added)
    assert any(isinstance(ent, EnphaseGatewayConsumptionMeterSensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_skips_dry_contacts_when_inventory_ready_and_absent(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseDryContactsInventorySensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            }
        },
        ["envoy"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(ent, EnphaseDryContactsInventorySensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_microinverter_site_entities_without_gateway_type(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseGatewayConsumptionMeterSensor,
        EnphaseGatewayProductionMeterSensor,
        EnphaseMicroinverterConnectivityStatusSensor,
        EnphaseMicroinverterLastReportedSensor,
        EnphaseMicroinverterReportingCountSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": 1,
                "devices": [{"serial_number": "INV-A"}],
                "status_counts": {"total": 1, "unknown": 1},
            }
        },
        ["microinverter"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(
        isinstance(ent, EnphaseMicroinverterConnectivityStatusSensor) for ent in added
    )
    assert any(
        isinstance(ent, EnphaseMicroinverterReportingCountSensor) for ent in added
    )
    assert any(isinstance(ent, EnphaseMicroinverterLastReportedSensor) for ent in added)
    assert not any(
        isinstance(ent, EnphaseGatewayProductionMeterSensor) for ent in added
    )
    assert not any(
        isinstance(ent, EnphaseGatewayConsumptionMeterSensor) for ent in added
    )


@pytest.mark.asyncio
async def test_async_setup_entry_adds_heatpump_site_entities(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseHeatPumpConnectivityStatusSensor,
        EnphaseHeatPumpEnergyMeterSensor,
        EnphaseHeatPumpLastReportedSensor,
        EnphaseHeatPumpPowerSensor,
        EnphaseHeatPumpSgReadyModeSensor,
        EnphaseHeatPumpStatusSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
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
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "SG_READY_GATEWAY",
                        "device_uid": "HP-SG-1",
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-EM-1",
                        "statusText": "Normal",
                    },
                ],
                "overall_status_text": "Normal",
            }
        },
        ["heatpump"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._heatpump_power_w = 640.0  # noqa: SLF001
    coord._heatpump_runtime_state = {  # noqa: SLF001
        "device_uid": "HP-1",
        "heatpump_status": "RUNNING",
        "sg_ready_mode_raw": "MODE_3",
        "sg_ready_mode_label": "Recommended",
        "sg_ready_active": True,
        "last_report_at": "2026-02-27T09:15:59Z",
    }
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(ent, EnphaseHeatPumpStatusSensor) for ent in added)
    assert any(
        isinstance(ent, EnphaseHeatPumpConnectivityStatusSensor) for ent in added
    )
    assert any(isinstance(ent, EnphaseHeatPumpSgReadyModeSensor) for ent in added)
    assert any(isinstance(ent, EnphaseHeatPumpEnergyMeterSensor) for ent in added)
    assert any(isinstance(ent, EnphaseHeatPumpLastReportedSensor) for ent in added)
    assert any(isinstance(ent, EnphaseHeatPumpPowerSensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_skips_runtime_only_heatpump_site_entities_without_dedicated_controller(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseHeatPumpConnectivityStatusSensor,
        EnphaseHeatPumpEnergyMeterSensor,
        EnphaseHeatPumpLastReportedSensor,
        EnphaseHeatPumpPowerSensor,
        EnphaseHeatPumpSgReadyModeSensor,
        EnphaseHeatPumpStatusSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
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
                        "statusText": "Normal",
                    },
                    {
                        "device_type": "ENERGY_METER",
                        "device_uid": "HP-EM-1",
                        "statusText": "Normal",
                    },
                ],
                "overall_status_text": "Normal",
            }
        },
        ["heatpump"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    coord._heatpump_power_w = 640.0  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert not any(isinstance(ent, EnphaseHeatPumpStatusSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseHeatPumpSgReadyModeSensor) for ent in added)
    assert not any(isinstance(ent, EnphaseHeatPumpLastReportedSensor) for ent in added)
    assert any(
        isinstance(ent, EnphaseHeatPumpConnectivityStatusSensor) for ent in added
    )
    assert any(isinstance(ent, EnphaseHeatPumpEnergyMeterSensor) for ent in added)
    assert any(isinstance(ent, EnphaseHeatPumpPowerSensor) for ent in added)


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_stale_heatpump_site_entities_when_unavailable(
    hass, config_entry, coordinator_factory
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"serial_number": "GW-1"}],
            }
        },
        ["envoy"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    ent_reg = er.async_get(hass)
    unique_id = f"{DOMAIN}_site_{coord.site_id}_heat_pump_status"
    entity_id = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id,
        suggested_object_id="heat_pump_status",
    ).entity_id
    assert ent_reg.async_get(entity_id) is not None

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert ent_reg.async_get(entity_id) is None


@pytest.mark.asyncio
async def test_async_setup_entry_keeps_grid_mode_when_controls_disabled(
    hass, config_entry, coordinator_factory
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import (
        EnphaseGridControlStatusSensor,
        EnphaseGridModeSensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord._grid_toggle_enabled = False  # noqa: SLF001
    coord._devices_inventory_ready = False  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    ent_reg = er.async_get(hass)
    entity_ids = [
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{DOMAIN}_site_{coord.site_id}_{key}",
            config_entry=config_entry,
        ).entity_id
        for key in ("grid_mode", "grid_control_status")
    ]
    added: list[Any] = []

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(entities),
    )

    assert ent_reg.async_get(entity_ids[0]) is not None
    assert ent_reg.async_get(entity_ids[1]) is None
    assert any(isinstance(entity, EnphaseGridModeSensor) for entity in added)
    assert not any(
        isinstance(entity, EnphaseGridControlStatusSensor) for entity in added
    )


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_stale_runtime_heatpump_site_entities_without_dedicated_controller(
    hass, config_entry, coordinator_factory
) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.enphase_ev.const import DOMAIN
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 2,
                "devices": [
                    {"device_type": "SG_READY_GATEWAY", "device_uid": "HP-SG-1"},
                    {"device_type": "ENERGY_METER", "device_uid": "HP-EM-1"},
                ],
            }
        },
        ["heatpump"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    ent_reg = er.async_get(hass)
    runtime_unique_ids = [
        f"{DOMAIN}_site_{coord.site_id}_heat_pump_status",
        f"{DOMAIN}_site_{coord.site_id}_heat_pump_sg_ready_mode",
        f"{DOMAIN}_site_{coord.site_id}_heat_pump_last_reported",
    ]
    entity_ids = [
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            unique_id,
            suggested_object_id=unique_id.rsplit("_", 1)[-1],
        ).entity_id
        for unique_id in runtime_unique_ids
    ]
    assert all(ent_reg.async_get(entity_id) is not None for entity_id in entity_ids)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    assert all(ent_reg.async_get(entity_id) is None for entity_id in entity_ids)


@pytest.mark.asyncio
async def test_async_setup_entry_adds_type_inventory_sensors(
    hass, config_entry, coordinator_factory
):
    from custom_components.enphase_ev.sensor import (
        EnphaseDryContactsInventorySensor,
        EnphaseTypeInventorySensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "wind_turbine": {
                "type_key": "wind_turbine",
                "type_label": "Wind Turbine",
                "count": 2,
                "devices": [{"name": "Wind 1"}, {"name": "Wind 2"}],
            },
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"name": "Battery 1"}],
            },
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 1,
                "devices": [{"name": "Garage Charger"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 1,
                "devices": [{"name": "Dry Contact 1", "statusText": "Closed"}],
            },
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": 1,
                "devices": [{"name": "Inverter 1"}],
            },
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [{"name": "Heat Pump 1"}],
            },
        },
        [
            "envoy",
            "wind_turbine",
            "encharge",
            "iqevse",
            "dry_contact",
            "microinverter",
            "heatpump",
        ],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert any(isinstance(ent, EnphaseDryContactsInventorySensor) for ent in added)
    type_entities = [
        ent for ent in added if isinstance(ent, EnphaseTypeInventorySensor)
    ]
    assert len(type_entities) == 1
    assert not any(ent._type_key == "encharge" for ent in type_entities)  # noqa: SLF001
    assert not any(ent._type_key == "iqevse" for ent in type_entities)  # noqa: SLF001
    assert not any(
        ent._type_key == "dry_contact" for ent in type_entities
    )  # noqa: SLF001
    assert not any(
        ent._type_key == "microinverter" for ent in type_entities
    )  # noqa: SLF001
    assert not any(ent._type_key == "heatpump" for ent in type_entities)  # noqa: SLF001
    wind = next(
        ent for ent in type_entities if ent._type_key == "wind_turbine"
    )  # noqa: SLF001
    assert wind.native_value == 2
    assert wind.extra_state_attributes["type_label"] == "Wind Turbine"
    assert wind.device_info["name"] == "Wind Turbine"


@pytest.mark.asyncio
async def test_async_setup_entry_type_inventory_listener_skips_known_non_dry_contact(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "wind_turbine": {
                "type_key": "wind_turbine",
                "type_label": "Wind Turbine",
                "count": 1,
                "devices": [{"name": "Wind 1"}],
            }
        },
        ["wind_turbine"],
    )
    callbacks: list[Any] = []

    def _add_listener(callback):
        callbacks.append(callback)
        return lambda: None

    coord.async_add_topology_listener = _add_listener  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    sync_topology_cb = next(
        cb for cb in callbacks if cb.__name__ == "_async_sync_topology"
    )
    before = len(added)
    sync_topology_cb()
    assert len(added) == before


@pytest.mark.asyncio
async def test_async_setup_entry_skips_gateway_and_battery_inventory_sensors(
    hass, config_entry, coordinator_factory
) -> None:
    from custom_components.enphase_ev.sensor import (
        EnphaseTypeInventorySensor,
        async_setup_entry,
    )

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"name": "Battery 1"}],
            },
        },
        ["envoy", "encharge"],
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    added: list[Any] = []

    def _capture(entities, update_before_add=False):
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    type_entities = [
        ent for ent in added if isinstance(ent, EnphaseTypeInventorySensor)
    ]
    assert type_entities == []


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_battery_type_inventory_entity(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "IQ Gateway"}],
            },
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 1,
                "devices": [{"name": "Battery 1"}],
            },
        },
        ["envoy", "encharge"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    battery_unique_id = f"enphase_ev_site_{coord.site_id}_type_encharge_inventory"
    fake_registry = SimpleNamespace(
        entities={
            "sensor.battery_inventory": SimpleNamespace(
                domain="sensor",
                entity_id="sensor.battery_inventory",
                platform="enphase_ev",
                config_entry_id=config_entry.entry_id,
                unique_id=battery_unique_id,
            ),
        },
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(return_value="sensor.battery_inventory"),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_remove.assert_any_call("sensor.battery_inventory")


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_stale_dry_contact_type_inventory_entity(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 1,
                "devices": [{"name": "Dry Contact 1", "statusText": "Closed"}],
            },
        },
        ["envoy", "dry_contact"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    stale_unique_id = f"enphase_ev_site_{coord.site_id}_type_dry_contact_inventory"
    invalid_suffix_unique_id = f"enphase_ev_site_{coord.site_id}_type_dry_contact_stale"
    non_dry_unique_id = f"enphase_ev_site_{coord.site_id}_type_encharge_inventory"
    fake_registry = SimpleNamespace(
        entities={
            "sensor.dry_contact_inventory": SimpleNamespace(
                domain="sensor",
                entity_id="sensor.dry_contact_inventory",
                platform="enphase_ev",
                config_entry_id=config_entry.entry_id,
                unique_id=stale_unique_id,
            ),
            "sensor.invalid_suffix": SimpleNamespace(
                domain="sensor",
                entity_id="sensor.invalid_suffix",
                platform="enphase_ev",
                config_entry_id=config_entry.entry_id,
                unique_id=invalid_suffix_unique_id,
            ),
            "sensor.non_dry": SimpleNamespace(
                domain="sensor",
                entity_id="sensor.non_dry",
                platform="enphase_ev",
                config_entry_id=config_entry.entry_id,
                unique_id=non_dry_unique_id,
            ),
        },
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_remove.assert_any_call("sensor.dry_contact_inventory")


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_legacy_drycontactloads_inventory_entity_id(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 1,
                "devices": [{"name": "Dry Contact 1", "statusText": "Closed"}],
            },
        },
        ["envoy", "dry_contact"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    fake_registry = SimpleNamespace(
        entities={
            "sensor.drycontactloads_drycontactloads_inventory": SimpleNamespace(
                domain="sensor",
                entity_id="sensor.drycontactloads_drycontactloads_inventory",
                platform="enphase_ev",
                config_entry_id=config_entry.entry_id,
                unique_id=None,
            )
        },
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    fake_registry.async_remove.assert_called_once_with(
        "sensor.drycontactloads_drycontactloads_inventory"
    )


@pytest.mark.asyncio
async def test_async_setup_entry_does_not_repeat_legacy_dry_contact_cleanup(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 1,
                "devices": [{"name": "Dry Contact 1", "statusText": "Closed"}],
            },
        },
        ["envoy", "dry_contact"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    expected_unique_id = f"enphase_ev_site_{coord.site_id}_type_dry_contact_inventory"
    fake_registry = SimpleNamespace(
        entities={},
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(
            side_effect=lambda domain, platform, unique_id: (
                "sensor.dry_contact_inventory"
                if domain == "sensor"
                and platform == "enphase_ev"
                and unique_id == expected_unique_id
                else None
            )
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    original = sensor_mod._is_dry_contact_type_key
    flag = {"enabled": False}
    monkeypatch.setattr(
        sensor_mod,
        "_is_dry_contact_type_key",
        lambda key: original(key) if flag["enabled"] else False,
    )
    callbacks: list[Any] = []
    coord.async_add_topology_listener = lambda cb: callbacks.append(cb) or (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    sync_type_cb = next(cb for cb in callbacks if cb.__name__ == "_async_sync_topology")
    flag["enabled"] = True
    sync_type_cb()

    fake_registry.async_remove.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_known_dry_contact_removal_handles_missing_entity_id(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 1,
                "devices": [{"name": "Dry Contact 1", "statusText": "Closed"}],
            },
        },
        ["envoy", "dry_contact"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    fake_registry = SimpleNamespace(
        entities={},
        async_remove=MagicMock(),
        async_get_entity_id=MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    original = sensor_mod._is_dry_contact_type_key
    flag = {"enabled": False}
    monkeypatch.setattr(
        sensor_mod,
        "_is_dry_contact_type_key",
        lambda key: original(key) if flag["enabled"] else False,
    )
    callbacks: list[Any] = []
    coord.async_add_topology_listener = lambda cb: callbacks.append(cb) or (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    sync_type_cb = next(cb for cb in callbacks if cb.__name__ == "_async_sync_topology")
    flag["enabled"] = True
    sync_type_cb()

    fake_registry.async_remove.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_known_dry_contact_removal_handles_noncallable_lookup(
    hass, config_entry, coordinator_factory, monkeypatch
) -> None:
    from custom_components.enphase_ev.sensor import async_setup_entry

    coord = coordinator_factory(serials=[])
    coord.inventory_runtime._set_type_device_buckets(  # noqa: SLF001
        {
            "envoy": {
                "type_key": "envoy",
                "type_label": "Gateway",
                "count": 1,
                "devices": [{"name": "System Controller", "channel_type": "enpower"}],
            },
            "dry_contact": {
                "type_key": "dry_contact",
                "type_label": "Dry Contact",
                "count": 1,
                "devices": [{"name": "Dry Contact 1", "statusText": "Closed"}],
            },
        },
        ["envoy", "dry_contact"],
    )
    coord._devices_inventory_ready = True  # noqa: SLF001
    fake_registry = SimpleNamespace(
        entities={},
        async_remove=MagicMock(),
        async_get_entity_id=None,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.sensor.er.async_get",
        lambda _hass: fake_registry,
    )

    original = sensor_mod._is_dry_contact_type_key
    flag = {"enabled": False}
    monkeypatch.setattr(
        sensor_mod,
        "_is_dry_contact_type_key",
        lambda key: original(key) if flag["enabled"] else False,
    )
    callbacks: list[Any] = []
    coord.async_add_topology_listener = lambda cb: callbacks.append(cb) or (lambda: None)  # type: ignore[assignment]
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    await async_setup_entry(hass, config_entry, lambda *_args, **_kwargs: None)

    sync_type_cb = next(cb for cb in callbacks if cb.__name__ == "_async_sync_topology")
    flag["enabled"] = True
    sync_type_cb()

    fake_registry.async_remove.assert_not_called()


def test_session_metadata_attributes_handle_blanks():
    attrs = sensor_mod.EnphaseEnergyTodaySensor._session_metadata_attributes(
        {},
        hass=None,
        context={},
        energy_kwh=None,
        energy_wh=None,
        duration_min=None,
        session_key=None,
    )
    assert attrs["energy_consumed_kwh"] is None
    assert attrs["energy_consumed_wh"] is None
    assert attrs["range_added"] is None
    assert attrs["session_cost"] is None
    assert attrs["session_duration_min"] is None
    assert "session_id" not in attrs
    assert attrs["session_started_at"] is None
    assert attrs["session_ended_at"] is None
    assert attrs["active_charge_time_s"] is None
    assert attrs["avg_cost_per_kwh"] is None
    assert attrs["cost_calculated"] is None
    assert attrs["session_cost_state"] is None
    assert attrs["manual_override"] is None
    assert attrs["charge_profile_stack_level"] is None


def test_session_metadata_attributes_formats_fields(monkeypatch):
    from homeassistant.const import UnitOfLength
    from homeassistant.util import dt as dt_util

    monkeypatch.setattr(
        dt_util, "as_local", lambda dt: dt.replace(tzinfo=timezone.utc)  # type: ignore[override]
    )
    payload = {
        "session_plug_in_at": 0,
        "session_plug_out_at": "2025-11-01T05:00:00Z",
        "session_miles": 10,
        "session_cost": "2.50",
        "session_charge_level": "24",
    }
    attrs = sensor_mod.EnphaseEnergyTodaySensor._session_metadata_attributes(
        payload,
        hass=SimpleNamespace(
            config=SimpleNamespace(
                units=SimpleNamespace(length_unit=UnitOfLength.KILOMETERS)
            )
        ),
        context={"energy_kwh": 1.5, "energy_wh": 1500.0, "session_charge_level": "20"},
        energy_kwh=1.5,
        energy_wh=1500.0,
        duration_min=90,
        session_key="abc",
    )
    assert attrs["plugged_in_at"].startswith("1970-01-01T00:00:00")
    assert attrs["plugged_out_at"] == "2025-11-01T05:00:00+00:00"
    assert attrs["energy_consumed_kwh"] == 1.5
    assert attrs["energy_consumed_wh"] == 1500.0
    assert attrs["session_cost"] == pytest.approx(2.5)
    assert attrs["session_charge_level"] == 20
    assert attrs["session_duration_min"] == 90
