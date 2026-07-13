from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call

import aiohttp
import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

import custom_components.enphase_ev as enphase_init
from custom_components.enphase_ev import (
    PLATFORMS,
    DOMAIN,
    _async_update_listener,
    _complete_startup_migrations_if_ready,
    _compose_charger_model_display,
    _entries_for_device,
    _find_entity_id_by_unique_id,
    _is_disabled_by_integration,
    _is_owned_entity,
    _iter_device_registry_entries,
    _iter_entity_registry_entries,
    _migrate_cloud_entity_unique_ids,
    _migrate_cloud_entities_to_cloud_device,
    _migrate_legacy_gateway_type_devices,
    _migrate_orphaned_update_entities_to_type_devices,
    _inventory_type_device_sw_version_for_registry,
    _normalize_selected_type_keys,
    _normalize_evse_model_name,
    _registry_charger_metadata_signature,
    _registry_metadata_signature,
    _registry_type_metadata_signature,
    _remove_retired_grid_profile_device_entities,
    _remove_evse_type_device_and_entities,
    _remove_legacy_inventory_entities,
    _remove_legacy_site_device,
    _startup_migration_version,
    _sync_charger_devices,
    _sync_registry_devices,
    _sync_type_devices,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.enphase_ev.const import (
    CONF_AUTH_BLOCK_REASON,
    CONF_AUTH_BLOCKED_UNTIL,
    CONF_INCLUDE_INVERTERS,
    CONF_SERIALS,
    CONF_SELECTED_TYPE_KEYS,
    CONF_SITE_ID,
    CONF_SITE_ONLY,
    ISSUE_AUTH_BLOCKED,
    ISSUE_TOO_MANY_ACTIVE_SESSIONS,
)
from custom_components.enphase_ev.device_types import type_identifier
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.services import async_setup_services
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL


def _with_inventory_view(coord):
    coord.inventory_view = SimpleNamespace(
        iter_type_keys=getattr(coord, "iter_type_keys", lambda: []),
        type_identifier=getattr(coord, "type_identifier", lambda _type_key: None),
        type_label=getattr(coord, "type_label", lambda _type_key: None),
        type_device_name=getattr(coord, "type_device_name", lambda _type_key: None),
        type_device_model=getattr(coord, "type_device_model", lambda _type_key: None),
        type_device_hw_version=getattr(
            coord, "type_device_hw_version", lambda _type_key: None
        ),
        type_device_serial_number=getattr(
            coord, "type_device_serial_number", lambda _type_key: None
        ),
        type_device_model_id=getattr(
            coord, "type_device_model_id", lambda _type_key: None
        ),
        type_device_sw_version=getattr(
            coord, "type_device_sw_version", lambda _type_key: None
        ),
    )
    if hasattr(coord, "type_device_sw_version_summary"):
        coord.inventory_view.type_device_sw_version_summary = (
            coord.type_device_sw_version_summary
        )
    return coord


def test_normalize_selected_type_keys_covers_string_and_fallback_paths() -> None:
    assert _normalize_selected_type_keys("envoy,\ninverters") == [
        "envoy",
        "microinverter",
    ]
    assert _normalize_selected_type_keys(123) == []


def test_remove_retired_grid_profile_entities_handles_current_sensor(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    _remove_retired_grid_profile_device_entities(hass, config_entry, None)

    site_id = config_entry.data[CONF_SITE_ID]
    registry = er.async_get(hass)
    current = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_current_grid_profile",
        config_entry=config_entry,
    )

    _remove_retired_grid_profile_device_entities(
        hass,
        config_entry,
        site_id,
    )
    assert registry.async_get(current.entity_id).entity_category is None

    monkeypatch.setattr(
        registry,
        "async_update_entity",
        Mock(side_effect=RuntimeError("registry unavailable")),
    )
    _remove_retired_grid_profile_device_entities(
        hass,
        config_entry,
        site_id,
    )


def test_startup_migration_version_returns_zero_for_invalid_value(config_entry) -> None:
    entry = SimpleNamespace(data={"startup_migration_version": "bad"})
    assert _startup_migration_version(entry) == 0


@pytest.mark.asyncio
async def test_async_setup_registers_services(hass: HomeAssistant, monkeypatch) -> None:
    setup_services = Mock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.async_setup_services", setup_services
    )

    assert await async_setup(hass, {})
    setup_services.assert_called_once()


def test_registry_metadata_signature_skips_dry_contact_and_handles_missing_helpers() -> (
    None
):
    coord = _with_inventory_view(
        SimpleNamespace(
            data={RANDOM_SERIAL: {"name": "Garage Charger", "sw_version": "1.0.0"}},
            iter_serials=lambda: [RANDOM_SERIAL],
            iter_type_keys=lambda: ["dry_contact_1", "iqevse"],
            type_identifier=lambda key: (DOMAIN, f"type:{key}"),
            type_label=lambda key: f"Label {key}",
            type_device_name=lambda key: f"Name {key}",
        )
    )

    type_signature = _registry_type_metadata_signature(coord)
    assert type_signature == ()

    charger_signature = _registry_charger_metadata_signature(coord)
    assert charger_signature == (
        (
            RANDOM_SERIAL,
            "Garage Charger",
            "Garage Charger",
            None,
            None,
            "1.0.0",
        ),
    )

    assert _registry_metadata_signature(coord) == (
        ("types", *type_signature),
        ("chargers", *charger_signature),
    )


def test_complete_startup_migrations_if_ready_ignores_failing_readiness_check(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)

    class DummyCoordinator:
        def startup_migrations_ready(self) -> bool:
            raise RuntimeError("boom")

    migrate_gateway = MagicMock()
    migrate_cloud = MagicMock()
    migrate_updates = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_legacy_gateway_type_devices",
        migrate_gateway,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_orphaned_update_entities_to_type_devices",
        migrate_updates,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_cloud_entities_to_cloud_device",
        migrate_cloud,
    )

    _complete_startup_migrations_if_ready(
        hass, config_entry, DummyCoordinator(), dev_reg, site_id
    )

    migrate_gateway.assert_not_called()
    migrate_updates.assert_not_called()
    migrate_cloud.assert_not_called()
    assert "startup_migration_version" not in config_entry.data


@pytest.mark.parametrize("update_result", [False, RuntimeError("boom")])
def test_complete_startup_migrations_clears_reload_suppression_when_update_not_applied(
    hass: HomeAssistant,
    config_entry,
    monkeypatch,
    update_result,
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=SimpleNamespace())

    class DummyCoordinator:
        def startup_migrations_ready(self) -> bool:
            return True

    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_cloud_entity_unique_ids",
        MagicMock(),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_legacy_gateway_type_devices",
        MagicMock(),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_orphaned_update_entities_to_type_devices",
        MagicMock(),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._remove_evse_type_device_and_entities",
        MagicMock(),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_cloud_entities_to_cloud_device",
        MagicMock(),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._remove_legacy_site_device",
        MagicMock(),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._remove_retired_grid_profile_device_entities",
        MagicMock(),
    )
    update_entry = MagicMock()
    if isinstance(update_result, BaseException):
        update_entry.side_effect = update_result
        monkeypatch.setattr(hass.config_entries, "async_update_entry", update_entry)
        with pytest.raises(RuntimeError, match="boom"):
            _complete_startup_migrations_if_ready(
                hass, config_entry, DummyCoordinator(), dev_reg, site_id
            )
    else:
        update_entry.return_value = update_result
        monkeypatch.setattr(hass.config_entries, "async_update_entry", update_entry)
        _complete_startup_migrations_if_ready(
            hass, config_entry, DummyCoordinator(), dev_reg, site_id
        )

    assert config_entry.runtime_data.reload_suppression_count == 0


def test_iter_device_registry_entries_handles_edge_paths() -> None:
    assert _iter_device_registry_entries(SimpleNamespace()) == []

    class BadDevices:
        def values(self):
            raise RuntimeError("boom")

    assert _iter_device_registry_entries(SimpleNamespace(devices=BadDevices())) == []

    class WeirdDict(dict):
        values = None

    entries = WeirdDict(
        {"a": SimpleNamespace(id="dev-1"), "b": SimpleNamespace(id="dev-2")}
    )
    assert _iter_device_registry_entries(SimpleNamespace(devices=entries)) == list(
        dict.values(entries)
    )
    assert (
        _iter_device_registry_entries(
            SimpleNamespace(devices=SimpleNamespace(values=None))
        )
        == []
    )


@pytest.mark.asyncio
async def test_migrate_orphaned_update_entities_to_type_devices_removes_site_updates(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    ent_reg = er.async_get(hass)
    owned_gateway = ent_reg.async_get_or_create(
        "update",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_envoy_firmware",
        config_entry=config_entry,
        original_name="Gateway Firmware",
    )
    owned_micro = ent_reg.async_get_or_create(
        "update",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_microinverter_firmware",
        config_entry=config_entry,
        original_name="Microinverter Firmware",
    )
    kept_charger = ent_reg.async_get_or_create(
        "update",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_iqevse_firmware",
        config_entry=config_entry,
        original_name="Charger Firmware",
    )

    _migrate_orphaned_update_entities_to_type_devices(hass, config_entry, site_id)

    assert ent_reg.async_get(owned_gateway.entity_id) is None
    assert ent_reg.async_get(owned_micro.entity_id) is None
    assert ent_reg.async_get(kept_charger.entity_id) is not None


def test_migrate_orphaned_update_entities_to_type_devices_handles_edge_paths(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    _migrate_orphaned_update_entities_to_type_devices(hass, config_entry, "   ")

    class _BadSiteId:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    _migrate_orphaned_update_entities_to_type_devices(hass, config_entry, _BadSiteId())

    monkeypatch.setattr("custom_components.enphase_ev.er", None)
    _migrate_orphaned_update_entities_to_type_devices(
        hass, config_entry, config_entry.data[CONF_SITE_ID]
    )

    class _BrokenEntityRegistryModule:
        @staticmethod
        def async_get(_hass):
            raise RuntimeError("boom")

    monkeypatch.setattr("custom_components.enphase_ev.er", _BrokenEntityRegistryModule)
    _migrate_orphaned_update_entities_to_type_devices(
        hass, config_entry, config_entry.data[CONF_SITE_ID]
    )

    removed: list[str] = []
    fake_registry = SimpleNamespace(
        entities={
            "foreign_owner": SimpleNamespace(
                entity_id="update.foreign_owner",
                platform=DOMAIN,
                domain="update",
                device_id=None,
                unique_id=f"{DOMAIN}_site_{config_entry.data[CONF_SITE_ID]}_envoy_firmware",
                config_entry_id="other-entry",
            ),
            "wrong_platform": SimpleNamespace(
                entity_id="update.wrong_platform",
                platform="other_platform",
                domain="update",
                device_id=None,
                unique_id=f"{DOMAIN}_site_{config_entry.data[CONF_SITE_ID]}_envoy_firmware",
                config_entry_id=config_entry.entry_id,
            ),
            "wrong_domain": SimpleNamespace(
                entity_id="sensor.wrong_domain",
                platform=DOMAIN,
                domain="sensor",
                device_id=None,
                unique_id=f"{DOMAIN}_site_{config_entry.data[CONF_SITE_ID]}_envoy_firmware",
                config_entry_id=config_entry.entry_id,
            ),
            "has_device": SimpleNamespace(
                entity_id="update.has_device",
                platform=DOMAIN,
                domain="update",
                device_id="device-1",
                unique_id=f"{DOMAIN}_site_{config_entry.data[CONF_SITE_ID]}_envoy_firmware",
                config_entry_id=config_entry.entry_id,
            ),
            "gateway_missing_entity_id": SimpleNamespace(
                entity_id=None,
                platform=DOMAIN,
                domain="update",
                device_id=None,
                unique_id=f"{DOMAIN}_site_{config_entry.data[CONF_SITE_ID]}_envoy_firmware",
                config_entry_id=config_entry.entry_id,
            ),
            "wrong_unique": SimpleNamespace(
                entity_id="update.wrong_unique",
                platform=DOMAIN,
                domain="update",
                device_id=None,
                unique_id=f"{DOMAIN}_site_{config_entry.data[CONF_SITE_ID]}_iqevse_firmware",
                config_entry_id=config_entry.entry_id,
            ),
            "missing_entity_id": SimpleNamespace(
                entity_id=None,
                platform=DOMAIN,
                domain="update",
                device_id=None,
                unique_id=f"{DOMAIN}_site_{config_entry.data[CONF_SITE_ID]}_microinverter_firmware",
                config_entry_id=config_entry.entry_id,
            ),
            "micro_with_device": SimpleNamespace(
                entity_id="update.micro_with_device",
                platform=DOMAIN,
                domain=None,
                device_id="device-1",
                unique_id=f"{DOMAIN}_site_{config_entry.data[CONF_SITE_ID]}_microinverter_firmware",
                config_entry_id=config_entry.entry_id,
            ),
        },
        async_remove=lambda entity_id: removed.append(entity_id),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er",
        SimpleNamespace(async_get=lambda _hass: fake_registry),
    )
    _migrate_orphaned_update_entities_to_type_devices(
        hass, config_entry, config_entry.data[CONF_SITE_ID]
    )
    assert removed == ["update.micro_with_device"]

    wrong_platform_registry = SimpleNamespace(
        entities={
            "wrong_platform": SimpleNamespace(
                entity_id="update.wrong_platform",
                platform="other_platform",
                domain="update",
                device_id=None,
                unique_id=f"{DOMAIN}_site_{config_entry.data[CONF_SITE_ID]}_envoy_firmware",
                config_entry_id=config_entry.entry_id,
            )
        },
        async_remove=lambda entity_id: removed.append(entity_id),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er",
        SimpleNamespace(async_get=lambda _hass: wrong_platform_registry),
    )
    removed.clear()
    _migrate_orphaned_update_entities_to_type_devices(
        hass, config_entry, config_entry.data[CONF_SITE_ID]
    )
    assert removed == []


@pytest.mark.asyncio
async def test_async_setup_entry_updates_existing_device(
    hass: HomeAssistant, config_entry, monkeypatch, mock_clientsession
) -> None:
    """Ensure charger devices are refreshed when registry data drifts."""
    site_id = config_entry.data[CONF_SITE_ID]
    device_registry = dr.async_get(hass)

    device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="LegacyVendor",
        name="Outdated Site",
        model="Old Model",
    )

    device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL)},
        manufacturer="LegacyVendor",
        name="Legacy Charger",
        model="Legacy Model",
        hw_version="0.1",
        sw_version="0.2",
    )

    class DummyCoordinator:
        def __init__(self) -> None:
            self.serials = {RANDOM_SERIAL}
            self.data = {
                RANDOM_SERIAL: {
                    "display_name": "Garage Charger",
                    "name": "Fallback Name",
                    "model_name": "IQ EVSE",
                    "hw_version": 321,
                    "sw_version": 654,
                    "model_id": "ignored",
                }
            }
            self.site_id = site_id
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return [RANDOM_SERIAL]

        def iter_type_keys(self) -> list[str]:
            return ["envoy", "iqevse"]

        def type_identifier(self, type_key: str):
            return type_identifier(self.site_id, type_key)

        def type_label(self, type_key: str) -> str:
            if type_key == "envoy":
                return "Gateway"
            return "EV Chargers"

        def type_device_name(self, type_key: str) -> str:
            if type_key == "envoy":
                return "Gateway (1)"
            return "EV Chargers (1)"

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    assert await async_setup_entry(hass, config_entry)
    await hass.async_block_till_done()
    args, kwargs = mock_clientsession.create_calls[-1]
    assert args == (hass,)
    assert kwargs["auto_cleanup"] is True
    assert isinstance(kwargs["cookie_jar"], aiohttp.DummyCookieJar)
    dummy_coord.schedule_sync.async_start.assert_awaited_once()
    forward.assert_awaited_once()

    updated = device_registry.async_get_device(identifiers={(DOMAIN, RANDOM_SERIAL)})
    assert updated is not None
    assert updated.name == "Garage Charger"
    assert updated.manufacturer == "Enphase"
    assert updated.model == "Garage Charger (IQ EVSE)"
    assert updated.hw_version == "321"
    assert updated.sw_version == "654"
    ev_type_device = device_registry.async_get_device(
        identifiers={(DOMAIN, f"type:{site_id}:iqevse")}
    )
    assert ev_type_device is None
    assert updated.via_device_id is None


@pytest.mark.asyncio
async def test_async_setup_entry_auth_block_is_setup_not_ready_without_reauth(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    """Startup auth blocks should retry setup instead of starting HA reauth."""

    blocked_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SERIALS: [],
            CONF_SITE_ONLY: True,
            CONF_AUTH_BLOCKED_UNTIL: blocked_until,
            CONF_AUTH_BLOCK_REASON: "login_wall_after_refresh_reject",
        },
    )
    object.__setattr__(
        config_entry, "state", config_entries.ConfigEntryState.SETUP_IN_PROGRESS
    )
    start_reauth = Mock()
    monkeypatch.setattr(config_entry, "async_start_reauth", start_reauth)
    status = AsyncMock()
    site_energy = AsyncMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseEVClient.status",
        status,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.energy.EnergyManager._async_refresh_site_energy",
        site_energy,
    )

    with pytest.raises(ConfigEntryNotReady) as exc_info:
        await async_setup_entry(hass, config_entry)

    assert isinstance(exc_info.value.__cause__, UpdateFailed)
    start_reauth.assert_not_called()
    status.assert_not_awaited()
    site_energy.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_setup_entry_restores_discovery_before_first_refresh(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    calls: list[str] = []

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.discovery_snapshot = SimpleNamespace(
                async_restore_state=self._async_restore_state
            )
            self.refresh_runner = SimpleNamespace(
                async_start_startup_power=self._async_start_power
            )

        async def _async_restore_state(self) -> None:
            calls.append("restore")

        async def _async_start_power(self) -> None:
            calls.append("power")

        async def async_config_entry_first_refresh(self) -> None:
            calls.append("refresh")

        def iter_serials(self) -> list[str]:
            return []

        def iter_type_keys(self) -> list[str]:
            return []

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())

    assert await async_setup_entry(hass, config_entry)

    assert calls == ["restore", "power", "refresh"]
    assert set(dummy_coord._setup_phase_timings) >= {
        "coordinator_init_s",
        "snapshot_restore_s",
        "translations_s",
        "integration_version_s",
        "first_refresh_s",
        "editor_sync_s",
        "registry_reconcile_s",
        "platform_forward_s",
        "total_s",
    }
    assert set(dummy_coord._setup_milestones) == {
        "core_ready",
        "entities_forwarded",
        "setup_complete",
    }


@pytest.mark.asyncio
async def test_async_setup_entry_uses_background_task_for_schedule_sync_start(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())
            self.refresh_runner = SimpleNamespace(
                async_start_startup_warmup=AsyncMock()
            )
            self.async_refresh_grid_profile_metadata = AsyncMock()
            self.track_entry_background_task = MagicMock()

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return []

        def iter_type_keys(self) -> list[str]:
            return []

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    background_calls: list[tuple[str, bool]] = []
    background_tasks: list[object] = []

    def _capture_background_task(target, name: str, eager_start: bool = True) -> object:
        background_calls.append((name, eager_start))
        target.close()
        task = object()
        background_tasks.append(task)
        return task

    monkeypatch.setattr(hass, "async_create_background_task", _capture_background_task)

    assert await async_setup_entry(hass, config_entry)

    assert background_calls == [
        ("enphase_ev_startup_warmup", True),
        ("enphase_ev_grid_profile_startup_probe", True),
        ("enphase_ev_schedule_sync_start", True),
    ]
    dummy_coord.refresh_runner.async_start_startup_warmup.assert_not_awaited()
    dummy_coord.async_refresh_grid_profile_metadata.assert_called_once_with(force=True)
    dummy_coord.async_refresh_grid_profile_metadata.assert_not_awaited()
    dummy_coord.schedule_sync.async_start.assert_not_awaited()
    assert [
        call(task) for task in background_tasks
    ] == dummy_coord.track_entry_background_task.call_args_list
    forward.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_setup_entry_registers_evse_schedule_editor_runtime(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    battery_editor = SimpleNamespace(sync_from_coordinator=Mock())
    evse_editor = SimpleNamespace(sync_from_coordinator=Mock())
    schedule_sync = SimpleNamespace(
        async_start=AsyncMock(), async_add_listener=Mock(return_value=lambda: None)
    )

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.schedule_sync = schedule_sync

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return []

        def iter_type_keys(self) -> list[str]:
            return []

        def async_add_listener(self, _callback):
            return lambda: None

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.battery_schedule_editor.BatteryScheduleEditorManager",
        lambda _coord: battery_editor,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.evse_schedule_editor.EvseScheduleEditorManager",
        lambda _coord: evse_editor,
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())

    assert await async_setup_entry(hass, config_entry)

    runtime_data = config_entry.runtime_data
    assert runtime_data.battery_schedule_editor is battery_editor
    assert runtime_data.evse_schedule_editor is evse_editor
    battery_editor.sync_from_coordinator.assert_called_once_with()
    evse_editor.sync_from_coordinator.assert_called_once_with()
    schedule_sync.async_add_listener.assert_called_once_with(
        evse_editor.sync_from_coordinator
    )


@pytest.mark.asyncio
async def test_async_setup_entry_uses_background_task_for_startup_warmup(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.refresh_runner = SimpleNamespace(
                async_start_startup_warmup=AsyncMock()
            )

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return []

        def iter_type_keys(self) -> list[str]:
            return []

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    background_calls: list[tuple[str, bool]] = []

    def _capture_background_task(target, name: str, eager_start: bool = True) -> None:
        background_calls.append((name, eager_start))
        target.close()

    monkeypatch.setattr(hass, "async_create_background_task", _capture_background_task)

    assert await async_setup_entry(hass, config_entry)

    assert background_calls == [("enphase_ev_startup_warmup", True)]
    dummy_coord.refresh_runner.async_start_startup_warmup.assert_not_awaited()
    forward.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_setup_entry_records_startup_migration_version(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return []

        def iter_type_keys(self) -> list[str]:
            return []

        def startup_migrations_ready(self) -> bool:
            return True

    dummy_coord = _with_inventory_view(DummyCoordinator())
    migrate_gateway = MagicMock()
    migrate_cloud = MagicMock()
    migrate_updates = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_legacy_gateway_type_devices",
        migrate_gateway,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_orphaned_update_entities_to_type_devices",
        migrate_updates,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_cloud_entities_to_cloud_device",
        migrate_cloud,
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())

    assert await async_setup_entry(hass, config_entry)

    migrate_gateway.assert_called_once()
    migrate_updates.assert_called_once()
    migrate_cloud.assert_called_once()
    assert config_entry.data["startup_migration_version"] == 6


@pytest.mark.asyncio
async def test_async_setup_entry_skips_startup_migrations_when_version_current(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, "startup_migration_version": 1},
    )

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return []

        def iter_type_keys(self) -> list[str]:
            return []

    dummy_coord = _with_inventory_view(DummyCoordinator())
    migrate_gateway = MagicMock()
    migrate_cloud = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_legacy_gateway_type_devices",
        migrate_gateway,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_cloud_entities_to_cloud_device",
        migrate_cloud,
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())

    assert await async_setup_entry(hass, config_entry)

    migrate_gateway.assert_not_called()
    migrate_cloud.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_skips_legacy_cleanup_when_migration_current(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, "startup_migration_version": 6},
    )
    dev_reg = dr.async_get(hass)
    legacy_site_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="Enphase",
        name=f"Enphase Site {site_id}",
        model="Site",
    )

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return []

        def iter_type_keys(self) -> list[str]:
            return ["envoy"]

        def type_identifier(self, type_key: str):
            return (DOMAIN, f"type:{site_id}:{type_key}")

        def type_label(self, _type_key: str) -> str:
            return "Gateway"

        def type_device_name(self, _type_key: str) -> str:
            return "IQ Gateway"

        def type_device_model(self, _type_key: str) -> str:
            return "IQ Gateway"

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())

    assert await async_setup_entry(hass, config_entry)

    assert dev_reg.async_get(legacy_site_device.id) is not None
    assert (
        dev_reg.async_get_device(identifiers={(DOMAIN, f"type:{site_id}:envoy")})
        is not None
    )


@pytest.mark.asyncio
async def test_remove_legacy_site_device_preserves_real_devices_with_site_identifier(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    gateway = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="IQ Gateway",
    )
    charger = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL), (DOMAIN, f"site:{site_id}")},
        manufacturer="Enphase",
        name="IQ EV Charger",
    )
    merged_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}"), ("other_domain", "device-1")},
        manufacturer="Enphase",
        name="Merged Site Device",
    )
    coord = _with_inventory_view(
        SimpleNamespace(
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
        )
    )

    _remove_legacy_site_device(hass, config_entry, coord, dev_reg, site_id)

    assert dev_reg.async_get(gateway.id) is not None
    assert dev_reg.async_get(charger.id) is not None
    assert dev_reg.async_get(merged_device.id) is not None


@pytest.mark.asyncio
async def test_remove_legacy_site_device_removes_empty_device_without_gateway(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    legacy_site_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="Enphase",
        name=f"Enphase Site {site_id}",
        model="Site",
    )
    coord = _with_inventory_view(
        SimpleNamespace(
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
        )
    )

    _remove_legacy_site_device(hass, config_entry, coord, dev_reg, site_id)

    assert dev_reg.async_get(legacy_site_device.id) is None


@pytest.mark.asyncio
async def test_remove_legacy_site_device_keeps_device_with_entities_without_gateway(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    legacy_site_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="Enphase",
        name=f"Enphase Site {site_id}",
        model="Site",
    )
    site_metric = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_legacy_site_metric_no_gateway",
        device_id=legacy_site_device.id,
        config_entry=config_entry,
    )
    coord = _with_inventory_view(
        SimpleNamespace(
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
        )
    )

    _remove_legacy_site_device(hass, config_entry, coord, dev_reg, site_id)

    assert dev_reg.async_get(legacy_site_device.id) is not None
    assert ent_reg.async_get(site_metric.entity_id) is not None


def test_remove_legacy_site_device_handles_guard_paths(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    coord = _with_inventory_view(
        SimpleNamespace(type_identifier=lambda _key: (DOMAIN, "type:site-1:envoy"))
    )
    noop_dev_reg = SimpleNamespace(async_get_device=lambda **_kwargs: None)

    monkeypatch.setattr(enphase_init, "er", None)
    _remove_legacy_site_device(hass, config_entry, coord, noop_dev_reg, "site-1")
    monkeypatch.setattr(enphase_init, "er", er)

    class BadSiteId:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    _remove_legacy_site_device(hass, config_entry, coord, noop_dev_reg, BadSiteId())
    _remove_legacy_site_device(hass, config_entry, coord, noop_dev_reg, "   ")

    gateway_no_id = SimpleNamespace(id=None)
    monkeypatch.setattr(enphase_init, "er", er)
    _remove_legacy_site_device(
        hass,
        config_entry,
        coord,
        SimpleNamespace(async_get_device=lambda **_kwargs: gateway_no_id),
        "site-1",
    )

    gateway = SimpleNamespace(id="gateway")

    def _device_reg_for(legacy_device):
        def _get_device(*, identifiers):
            if (DOMAIN, "type:site-1:envoy") in identifiers:
                return gateway
            if (DOMAIN, "site:site-1") in identifiers:
                return legacy_device
            return None

        return SimpleNamespace(async_get_device=_get_device)

    legacy_other_entry = SimpleNamespace(
        id="legacy",
        identifiers={(DOMAIN, "site:site-1")},
        config_entries={"other-entry"},
    )
    _remove_legacy_site_device(
        hass, config_entry, coord, _device_reg_for(legacy_other_entry), "site-1"
    )

    legacy_no_id = SimpleNamespace(
        id=None,
        identifiers={(DOMAIN, "site:site-1")},
        config_entries={config_entry.entry_id},
    )
    _remove_legacy_site_device(
        hass, config_entry, coord, _device_reg_for(legacy_no_id), "site-1"
    )

    legacy_same_id = SimpleNamespace(
        id="gateway",
        identifiers={(DOMAIN, "site:site-1")},
        config_entries={config_entry.entry_id},
    )
    _remove_legacy_site_device(
        hass, config_entry, coord, _device_reg_for(legacy_same_id), "site-1"
    )

    legacy = SimpleNamespace(
        id="legacy",
        identifiers={(DOMAIN, "site:site-1")},
        config_entries={config_entry.entry_id},
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: (_ for _ in ()).throw(RuntimeError("registry failed")),
    )
    _remove_legacy_site_device(
        hass, config_entry, coord, _device_reg_for(legacy), "site-1"
    )

    entry_without_entity_id = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id=config_entry.entry_id,
        entity_id=None,
        device_id="legacy",
    )
    ent_reg_missing_entity_id = SimpleNamespace(
        entities={"entry": entry_without_entity_id},
        async_update_entity=Mock(),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: ent_reg_missing_entity_id,
    )
    _remove_legacy_site_device(
        hass, config_entry, coord, _device_reg_for(legacy), "site-1"
    )

    entry_with_entity_id = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id=config_entry.entry_id,
        entity_id="sensor.legacy_site_metric",
        device_id="legacy",
    )
    ent_reg_update_failure = SimpleNamespace(
        entities={"entry": entry_with_entity_id},
        async_update_entity=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("move failed")
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: ent_reg_update_failure,
    )
    _remove_legacy_site_device(
        hass, config_entry, coord, _device_reg_for(legacy), "site-1"
    )


@pytest.mark.asyncio
async def test_async_setup_entry_schedule_sync_falls_back_to_entry_background_task(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return []

        def iter_type_keys(self) -> list[str]:
            return []

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(hass, "async_create_background_task", None)

    calls: list[tuple[HomeAssistant, str, bool]] = []

    def _capture_entry_background_task(
        hass_arg: HomeAssistant, target, name: str, eager_start: bool = True
    ):
        calls.append((hass_arg, name, eager_start))
        target.close()
        return None

    monkeypatch.setattr(
        config_entry, "async_create_background_task", _capture_entry_background_task
    )

    assert await async_setup_entry(hass, config_entry)

    assert calls == [(hass, "enphase_ev_schedule_sync_start", True)]
    dummy_coord.schedule_sync.async_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_setup_entry_schedule_sync_falls_back_to_hass_create_task(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return []

        def iter_type_keys(self) -> list[str]:
            return []

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(config_entry, "async_create_background_task", None)
    monkeypatch.setattr(hass, "async_create_background_task", None)
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, "startup_migration_version": 1},
    )

    created: list[str] = []

    def _capture_hass_create_task(target, name=None):
        created.append("created")
        target.close()
        return None

    monkeypatch.setattr(hass, "async_create_task", _capture_hass_create_task)

    assert await async_setup_entry(hass, config_entry)

    assert created == ["created"]
    dummy_coord.schedule_sync.async_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_setup_entry_updates_title_to_prefixed_site_id(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return []

        def iter_type_keys(self) -> list[str]:
            return []

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    original_update = hass.config_entries.async_update_entry
    update_calls: list[dict[str, object]] = []

    def capture_update(entry, **kwargs):
        update_calls.append(kwargs)
        return original_update(entry, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", capture_update)

    assert await async_setup_entry(hass, config_entry)
    expected_title = f"Site: {site_id}"
    assert any(call.get("title") == expected_title for call in update_calls)
    assert config_entry.title == expected_title


@pytest.mark.asyncio
async def test_async_setup_entry_migrates_selected_type_keys_for_microinverters_only(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SELECTED_TYPE_KEYS: ["envoy", "encharge"],
            CONF_INCLUDE_INVERTERS: True,
        },
    )

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return []

        def iter_type_keys(self) -> list[str]:
            return []

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    original_update = hass.config_entries.async_update_entry
    update_calls: list[dict[str, object]] = []

    def capture_update(entry, **kwargs):
        update_calls.append(kwargs)
        return original_update(entry, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", capture_update)

    assert await async_setup_entry(hass, config_entry)

    assert any(
        call.get("data", {}).get(CONF_SELECTED_TYPE_KEYS)
        == ["envoy", "encharge", "microinverter"]
        for call in update_calls
    )
    assert config_entry.data[CONF_SELECTED_TYPE_KEYS] == [
        "envoy",
        "encharge",
        "microinverter",
    ]


@pytest.mark.asyncio
async def test_async_setup_entry_does_not_add_heatpump_without_gateway_selection(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_SELECTED_TYPE_KEYS: ["iqevse"],
            CONF_INCLUDE_INVERTERS: False,
        },
    )

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return []

        def iter_type_keys(self) -> list[str]:
            return []

    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: _with_inventory_view(
            DummyCoordinator()
        ),
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())

    assert await async_setup_entry(hass, config_entry)
    assert config_entry.data[CONF_SELECTED_TYPE_KEYS] == ["iqevse"]


@pytest.mark.asyncio
async def test_async_setup_entry_model_display_variants(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    """Ensure model metadata covers display-only and model-only chargers."""
    device_registry = dr.async_get(hass)
    device_registry.async_clear_config_entry(config_entry.entry_id)
    hass.data.pop(DOMAIN, None)

    site_id = config_entry.data[CONF_SITE_ID]

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.serials = {"MODEL_ONLY", "DISPLAY_ONLY"}
            self.data = {
                "MODEL_ONLY": {
                    "model_name": "IQ EVSE",
                },
                "DISPLAY_ONLY": {
                    "display_name": "Workshop Charger",
                },
            }

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return ["MODEL_ONLY", "DISPLAY_ONLY"]

        def iter_type_keys(self) -> list[str]:
            return ["iqevse"]

        def type_identifier(self, type_key: str):
            return type_identifier(self.site_id, type_key)

        def type_label(self, _type_key: str) -> str:
            return "EV Chargers"

        def type_device_name(self, _type_key: str) -> str:
            return "EV Chargers (2)"

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    assert await async_setup_entry(hass, config_entry)

    model_device = device_registry.async_get_device(
        identifiers={(DOMAIN, "MODEL_ONLY")}
    )
    display_device = device_registry.async_get_device(
        identifiers={(DOMAIN, "DISPLAY_ONLY")}
    )

    assert model_device is not None
    assert model_device.model == "IQ EVSE"
    assert display_device is not None
    assert display_device.model == "Workshop Charger"


@pytest.mark.asyncio
async def test_async_setup_entry_uses_fallback_name_for_model(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    device_registry = dr.async_get(hass)
    device_registry.async_clear_config_entry(config_entry.entry_id)
    hass.data.pop(DOMAIN, None)

    site_id = config_entry.data[CONF_SITE_ID]

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.serials = {"FALLBACK_ONLY"}
            self.data = {
                "FALLBACK_ONLY": {
                    "name": "Fallback Charger",
                },
            }

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return ["FALLBACK_ONLY"]

        def iter_type_keys(self) -> list[str]:
            return ["iqevse"]

        def type_identifier(self, type_key: str):
            return type_identifier(self.site_id, type_key)

        def type_label(self, _type_key: str) -> str:
            return "EV Chargers"

        def type_device_name(self, _type_key: str) -> str:
            return "EV Chargers (1)"

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    assert await async_setup_entry(hass, config_entry)

    device = device_registry.async_get_device(identifiers={(DOMAIN, "FALLBACK_ONLY")})
    assert device is not None
    assert device.model == "Fallback Charger"


@pytest.mark.asyncio
async def test_async_setup_entry_registry_sync_listener_handles_exceptions(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    topology_listeners: list = []
    state_listeners: list = []
    state_listeners: list = []

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.serials = {RANDOM_SERIAL}
            self.data = {RANDOM_SERIAL: {"name": "Fallback Charger"}}
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return [RANDOM_SERIAL]

        def iter_type_keys(self) -> list[str]:
            return ["iqevse"]

        def type_identifier(self, type_key: str):
            return type_identifier(self.site_id, type_key)

        def type_label(self, _type_key: str) -> str:
            return "EV Chargers"

        def type_device_name(self, _type_key: str) -> str:
            return "EV Chargers (1)"

        def async_add_topology_listener(self, callback):
            topology_listeners.append(callback)
            return lambda: None

        def async_add_listener(self, callback):
            state_listeners.append(callback)
            return lambda: None

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)

    assert await async_setup_entry(hass, config_entry)
    assert topology_listeners, "expected setup to register a topology listener"
    assert state_listeners, "expected setup to register a state listener"

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("custom_components.enphase_ev._sync_registry_devices", _boom)
    dummy_coord.data[RANDOM_SERIAL]["sw_version"] = "1.2.3"
    state_listeners[0]()  # should swallow and log internal sync exceptions


@pytest.mark.asyncio
async def test_async_unload_entry_stops_schedule_sync(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    schedule_sync = SimpleNamespace(async_stop=AsyncMock())
    coord = SimpleNamespace(
        schedule_sync=schedule_sync,
        async_cleanup_runtime_state=AsyncMock(),
        async_close=AsyncMock(),
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    unload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_unload", unload)

    assert await async_unload_entry(hass, config_entry)
    schedule_sync.async_stop.assert_awaited_once()
    coord.async_cleanup_runtime_state.assert_awaited_once()
    coord.async_close.assert_awaited_once()
    assert unload.await_count == len(PLATFORMS)
    assert config_entry.runtime_data is None


class _RecordingTask:
    def __init__(self, *, done: bool = False) -> None:
        self._done = done
        self.cancelled = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self.cancelled = True


@pytest.mark.asyncio
async def test_async_unload_entry_cancels_background_lifecycle_tasks(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    warmup_task = _RecordingTask()
    session_task = _RecordingTask()
    amp_task = _RecordingTask()
    completed_amp_task = _RecordingTask(done=True)
    stream_stop_task = _RecordingTask()
    auth_refresh_task = _RecordingTask()
    battery_restore_task = _RecordingTask()
    backoff_refresh_task = _RecordingTask()
    backoff_cancel = Mock()
    schedule_sync = SimpleNamespace(async_stop=AsyncMock())
    session_history = SimpleNamespace(_enrichment_tasks={session_task})

    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    def _clear_session_history() -> None:
        for task in list(session_history._enrichment_tasks):
            task.cancel()
        session_history._enrichment_tasks.clear()

    session_history.clear = Mock(side_effect=_clear_session_history)

    class CleanupCoordinator:
        cleanup_runtime_state = EnphaseCoordinator.cleanup_runtime_state

        def __init__(self) -> None:
            self._warmup_task = warmup_task
            self._amp_restart_tasks = {
                "EV123": amp_task,
                "EV456": completed_amp_task,
            }
            self._streaming_stop_task = stream_stop_task
            self._auth_refresh_task = auth_refresh_task
            self._battery_profile_recovery_restore_task = battery_restore_task
            self._backoff_cancel = backoff_cancel
            self._backoff_refresh_tasks = {backoff_refresh_task}
            self._backoff_until = 123.0
            self.backoff_ends_utc = datetime.now(timezone.utc)
            self.discovery_snapshot = SimpleNamespace(cancel_pending_save=Mock())
            self.session_history = session_history
            self._session_history_cache_shim = {("EV123", "2026-04-24"): (1.0, [])}
            self._topology_listeners = [Mock()]
            self.schedule_sync = schedule_sync
            self.evse_runtime = SimpleNamespace(
                prune_runtime_caches=Mock(),
            )

        def _clear_backoff_timer(self) -> None:
            EnphaseCoordinator._clear_backoff_timer(self)

        def _prune_runtime_caches(self, *, active_serials, keep_day_keys) -> None:
            self.evse_runtime.prune_runtime_caches(
                active_serials=active_serials,
                keep_day_keys=keep_day_keys,
            )

    coord = CleanupCoordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    unload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_unload", unload)

    assert await async_unload_entry(hass, config_entry)

    schedule_sync.async_stop.assert_awaited_once()
    assert warmup_task.cancelled
    assert session_task.cancelled
    assert amp_task.cancelled
    assert not completed_amp_task.cancelled
    assert stream_stop_task.cancelled
    assert auth_refresh_task.cancelled
    assert battery_restore_task.cancelled
    assert backoff_refresh_task.cancelled
    backoff_cancel.assert_called_once_with()
    assert coord._warmup_task is None
    assert coord._amp_restart_tasks == {}
    assert coord._streaming_stop_task is None
    assert coord._auth_refresh_task is None
    assert coord._battery_profile_recovery_restore_task is None
    assert coord._backoff_cancel is None
    assert coord._backoff_refresh_tasks == set()
    assert coord._backoff_until is None
    assert coord._session_history_cache_shim == {}
    assert coord._topology_listeners == []
    session_history.clear.assert_called_once_with()
    coord.discovery_snapshot.cancel_pending_save.assert_called_once_with()
    coord.evse_runtime.prune_runtime_caches.assert_called_once_with(
        active_serials=(), keep_day_keys=()
    )
    assert config_entry.runtime_data is None


@pytest.mark.asyncio
async def test_async_unload_entry_does_not_cleanup_when_unload_fails(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    schedule_sync = SimpleNamespace(async_stop=AsyncMock())
    coord = SimpleNamespace(
        schedule_sync=schedule_sync, cleanup_runtime_state=MagicMock()
    )
    runtime_data = EnphaseRuntimeData(coordinator=coord)
    config_entry.runtime_data = runtime_data

    async def unload(_entry, platform):
        return platform != "calendar"

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_unload", unload)

    assert await async_unload_entry(hass, config_entry) is False
    schedule_sync.async_stop.assert_not_awaited()
    coord.cleanup_runtime_state.assert_not_called()
    assert config_entry.runtime_data is runtime_data


@pytest.mark.asyncio
async def test_async_unload_entry_handles_missing_runtime_data(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    unload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_unload", unload)

    assert await async_unload_entry(hass, config_entry)
    assert unload.await_count == len(PLATFORMS)


@pytest.mark.asyncio
async def test_update_listener_reloads_entry(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    reload = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    object.__setattr__(config_entry, "state", config_entries.ConfigEntryState.LOADED)

    await _async_update_listener(hass, config_entry)

    reload.assert_awaited_once_with(config_entry.entry_id)


@pytest.mark.asyncio
async def test_update_listener_skips_reload_for_disabled_entry(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    reload = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    object.__setattr__(config_entry, "state", config_entries.ConfigEntryState.LOADED)
    object.__setattr__(config_entry, "disabled_by", "user")

    await _async_update_listener(hass, config_entry)

    reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_listener_skips_reload_when_not_loaded(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    reload = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    object.__setattr__(
        config_entry, "state", config_entries.ConfigEntryState.FAILED_UNLOAD
    )

    await _async_update_listener(hass, config_entry)

    reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_listener_ignores_operation_not_allowed(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    reload = AsyncMock(side_effect=config_entries.OperationNotAllowed("race"))
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    object.__setattr__(config_entry, "state", config_entries.ConfigEntryState.LOADED)

    await _async_update_listener(hass, config_entry)

    reload.assert_awaited_once_with(config_entry.entry_id)


@pytest.mark.asyncio
async def test_async_unload_entry_tolerates_platform_never_loaded(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    schedule_sync = SimpleNamespace(async_stop=AsyncMock())
    coord = SimpleNamespace(
        schedule_sync=schedule_sync, cleanup_runtime_state=MagicMock()
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    async def unload(_entry, platform):
        if platform == "calendar":
            raise ValueError("Config entry was never loaded!")
        return True

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_unload", unload)

    assert await async_unload_entry(hass, config_entry)

    schedule_sync.async_stop.assert_awaited_once()
    coord.cleanup_runtime_state.assert_called_once()
    assert config_entry.runtime_data is None


@pytest.mark.asyncio
async def test_async_unload_entry_reraises_unexpected_value_error(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    schedule_sync = SimpleNamespace(async_stop=AsyncMock())
    coord = SimpleNamespace(
        schedule_sync=schedule_sync, cleanup_runtime_state=MagicMock()
    )
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    async def unload(_entry, platform):
        if platform == "calendar":
            raise ValueError("unexpected")
        return True

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_unload", unload)

    with pytest.raises(ValueError, match="unexpected"):
        await async_unload_entry(hass, config_entry)

    schedule_sync.async_stop.assert_not_awaited()
    coord.cleanup_runtime_state.assert_not_called()


@pytest.mark.asyncio
async def test_registered_services_cover_branches(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    """Exercise service handlers to cover edge cases in helpers."""
    site_id = config_entry.data[CONF_SITE_ID]
    device_registry = dr.async_get(hass)
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)

    fake_ir_deletes: list[str] = []
    monkeypatch.setattr(
        "custom_components.enphase_ev.services.ir",
        SimpleNamespace(
            async_delete_issue=lambda hass_, domain, issue_id: fake_ir_deletes.append(
                issue_id
            )
        ),
    )

    class FakeHAService:
        def __init__(self) -> None:
            self.calls = 0
            self.referenced_device_ids: list[str] = []

        def async_extract_referenced_device_ids(self, hass_, call):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            return list(self.referenced_device_ids)

    fake_service_helper = FakeHAService()
    monkeypatch.setattr(
        "custom_components.enphase_ev.services.ha_service", fake_service_helper
    )

    site_device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="Enphase",
        name="Garage Site",
    )
    first_serial = RANDOM_SERIAL
    second_serial = "EV0002"

    charger_one = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, first_serial)},
        manufacturer="Enphase",
        name="Driveway Charger",
        via_device=(DOMAIN, f"site:{site_id}"),
    )
    charger_two = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={
            (DOMAIN, second_serial),
            (DOMAIN, f"site:{site_id}"),
        },
        manufacturer="Enphase",
        name="Garage Charger B",
    )
    lonely_device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "EV4040")},
        manufacturer="Enphase",
        name="Lonely Charger",
    )
    other_site_device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "site:other-site")},
        manufacturer="Enphase",
        name="Other Site",
    )

    class FakeCoordinator:
        def __init__(self, site, serials, data, start_results, *, site_only=False):
            self.site_id = site
            self.serials = set(serials)
            self.data = data
            self.site_only = site_only
            self._start_results = start_results
            self._streaming = False
            self.schedule_sync = SimpleNamespace(async_refresh=AsyncMock())

            async def _start(sn, **_kwargs):
                return self._start_results[sn]

            self.async_start_charging = AsyncMock(side_effect=_start)
            self.async_stop_charging = AsyncMock(return_value=None)
            self.async_trigger_ocpp_message = AsyncMock(
                side_effect=lambda sn, message: {"sent": message, "sn": sn}
            )

            async def _start_streaming(*_args, **_kwargs):
                self._streaming = True
                return None

            async def _stop_streaming(*_args, **_kwargs):
                self._streaming = False
                return None

            self.async_start_streaming = AsyncMock(side_effect=_start_streaming)
            self.async_stop_streaming = AsyncMock(side_effect=_stop_streaming)
            self.async_request_grid_toggle_otp = AsyncMock(return_value=None)
            self.async_set_grid_mode = AsyncMock(return_value=None)
            self.async_update_cfg_schedule = AsyncMock(return_value=None)

            self.client = SimpleNamespace(
                start_live_stream=AsyncMock(return_value=None),
                stop_live_stream=AsyncMock(return_value=None),
            )
            self.async_request_refresh = AsyncMock()

    coord_site_only = FakeCoordinator(
        site_id,
        serials=set(),
        data={},
        start_results={},
        site_only=True,
    )
    coord_primary = FakeCoordinator(
        site_id,
        serials={second_serial},
        data={first_serial: {}, second_serial: {}},
        start_results={
            first_serial: {"status": "not_ready"},
            second_serial: {"status": "ok"},
        },
    )
    coord_duplicate = FakeCoordinator(
        site_id,
        serials={"unused"},
        data={},
        start_results={},
    )
    coord_other = FakeCoordinator(
        "other-site",
        serials={"EV9999"},
        data={"EV9999": {}},
        start_results={"EV9999": {"status": "ok"}},
    )

    entry_one = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: site_id},
        title="Primary Site",
        unique_id="entry-one",
    )
    entry_one.add_to_hass(hass)
    entry_one.runtime_data = EnphaseRuntimeData(coordinator=coord_primary)

    entry_two = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: site_id},
        title="Duplicate Site",
        unique_id="entry-two",
    )
    entry_two.add_to_hass(hass)
    entry_two.runtime_data = EnphaseRuntimeData(coordinator=coord_duplicate)

    entry_three = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "other-site"},
        title="Other Site",
        unique_id="entry-three",
    )
    entry_three.add_to_hass(hass)
    entry_three.runtime_data = EnphaseRuntimeData(coordinator=coord_other)

    async_setup_services(hass)

    svc_start = registered[(DOMAIN, "start_charging")]["handler"]
    svc_stop = registered[(DOMAIN, "stop_charging")]["handler"]
    svc_trigger = registered[(DOMAIN, "trigger_message")]["handler"]
    svc_clear = registered[(DOMAIN, "clear_reauth_issue")]["handler"]
    svc_start_stream = registered[(DOMAIN, "start_live_stream")]["handler"]
    svc_stop_stream = registered[(DOMAIN, "stop_live_stream")]["handler"]
    svc_sync = registered[(DOMAIN, "sync_schedules")]["handler"]
    svc_request_grid_otp = registered[(DOMAIN, "request_grid_toggle_otp")]["handler"]
    svc_set_grid_mode = registered[(DOMAIN, "set_grid_mode")]["handler"]
    svc_update_cfg = registered[(DOMAIN, "update_cfg_schedule")]["handler"]
    update_cfg_schema = registered[(DOMAIN, "update_cfg_schedule")]["schema"]

    from custom_components.enphase_ev.coordinator import ServiceValidationError

    with pytest.raises(ServiceValidationError):
        await svc_start(SimpleNamespace(data={}))
    with pytest.raises(ServiceValidationError):
        await svc_stop(SimpleNamespace(data={}))

    fake_service_helper.calls = 0
    with pytest.raises(ServiceValidationError):
        await svc_trigger(SimpleNamespace(data={"requested_message": "MeterValues"}))

    with pytest.raises(vol.Invalid):
        update_cfg_schema({"site_id": site_id})
    assert update_cfg_schema({"site_id": site_id, "limit": 75}) == {
        "site_id": site_id,
        "limit": 75,
    }

    with pytest.raises(ServiceValidationError):
        await svc_start(SimpleNamespace(data={"device_id": [lonely_device.id]}))
    with pytest.raises(ServiceValidationError):
        await svc_stop(SimpleNamespace(data={"device_id": lonely_device.id}))
    with pytest.raises(ServiceValidationError):
        await svc_trigger(
            SimpleNamespace(
                data={
                    "device_id": lonely_device.id,
                    "requested_message": "MeterValues",
                }
            )
        )

    await svc_sync(SimpleNamespace(data={"device_id": [charger_two.id]}))
    assert call(reason="service", serials=[second_serial]) in (
        coord_primary.schedule_sync.async_refresh.await_args_list
    )
    fake_service_helper.referenced_device_ids = ["missing-device-id"]
    with pytest.raises(ServiceValidationError):
        await svc_sync(
            SimpleNamespace(data={"device_id": [charger_two.id, site_device.id]})
        )
    fake_service_helper.referenced_device_ids = []
    with pytest.raises(ServiceValidationError):
        await svc_sync(SimpleNamespace(data={"device_id": [lonely_device.id]}))
    await svc_request_grid_otp(SimpleNamespace(data={"site_id": site_id}))
    coord_primary.async_request_grid_toggle_otp.assert_awaited_once()
    coord_primary.async_request_refresh.assert_awaited()

    await svc_set_grid_mode(
        SimpleNamespace(data={"site_id": site_id, "mode": "off_grid", "otp": "1234"})
    )
    coord_primary.async_set_grid_mode.assert_awaited_once_with("off_grid", "1234")
    await svc_update_cfg(
        SimpleNamespace(data={"site_id": site_id, "limit": 80, "start_time": "01:00"})
    )
    coord_primary.async_update_cfg_schedule.assert_awaited_once_with(
        start="01:00",
        end=None,
        limit=80,
    )

    entry_site_only = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: site_id},
        title="Site Only",
        unique_id="entry-site-only",
    )
    entry_site_only.add_to_hass(hass)
    entry_site_only.runtime_data = EnphaseRuntimeData(coordinator=coord_site_only)

    start_call = SimpleNamespace(
        data={
            "device_id": [charger_one.id, charger_two.id],
            "charging_level": 30,
            "connector_id": 2,
        }
    )
    await svc_start(start_call)

    await_args = coord_primary.async_start_charging.await_args_list
    assert call(first_serial, requested_amps=30, connector_id=2) in await_args
    assert call(second_serial, requested_amps=30, connector_id=2) in await_args
    assert coord_primary.async_start_charging.await_count == 2
    coord_site_only.async_start_charging.assert_not_awaited()
    fake_service_helper.referenced_device_ids = ["missing-device-id"]
    with pytest.raises(ServiceValidationError):
        await svc_start(
            SimpleNamespace(
                data={
                    "device_id": [charger_one.id, site_device.id, charger_two.id],
                    "charging_level": 30,
                    "connector_id": 2,
                }
            )
        )
    fake_service_helper.referenced_device_ids = []

    stop_call = SimpleNamespace(data={"device_id": charger_one.id})
    await svc_stop(stop_call)
    coord_primary.async_stop_charging.assert_awaited_once_with(first_serial)
    coord_site_only.async_stop_charging.assert_not_awaited()

    trigger_call = SimpleNamespace(
        data={"device_id": charger_two.id, "requested_message": "MeterValues"}
    )
    trigger_result = await svc_trigger(trigger_call)
    assert trigger_result["results"] == [
        {
            "device_id": charger_two.id,
            "serial": second_serial,
            "site_id": site_id,
            "response": {"sent": "MeterValues", "sn": second_serial},
        }
    ]
    coord_primary.async_trigger_ocpp_message.assert_awaited_once_with(
        second_serial, "MeterValues"
    )

    clear_call = SimpleNamespace(
        data={"device_id": [charger_one.id], "site_id": "explicit-site"}
    )
    await svc_clear(clear_call)
    assert set(fake_ir_deletes) == {
        "auth_blocked",
        f"{ISSUE_AUTH_BLOCKED}_{site_id}",
        f"{ISSUE_AUTH_BLOCKED}_explicit-site",
        ISSUE_TOO_MANY_ACTIVE_SESSIONS,
        f"{ISSUE_TOO_MANY_ACTIVE_SESSIONS}_{site_id}",
        f"{ISSUE_TOO_MANY_ACTIVE_SESSIONS}_explicit-site",
        "reauth_required",
        f"reauth_required_{site_id}",
        "reauth_required_explicit-site",
    }

    await svc_start_stream(SimpleNamespace(data={"site_id": site_id}))
    await svc_start_stream(SimpleNamespace(data={"device_id": [charger_one.id]}))
    with pytest.raises(ServiceValidationError):
        await svc_start_stream(SimpleNamespace(data={}))
    coord_primary.async_start_streaming.assert_awaited()
    assert coord_other.async_start_streaming.await_count == 0
    assert coord_primary._streaming is True

    await svc_stop_stream(SimpleNamespace(data={"site_id": site_id}))
    await svc_stop_stream(SimpleNamespace(data={"device_id": [charger_one.id]}))
    with pytest.raises(ServiceValidationError):
        await svc_stop_stream(SimpleNamespace(data={}))
    coord_primary.async_stop_streaming.assert_awaited()
    assert coord_other.async_stop_streaming.await_count == 0
    assert coord_primary._streaming is False

    fake_service_helper.calls = 0
    with pytest.raises(ServiceValidationError):
        await svc_sync(SimpleNamespace(data={}))
    assert coord_primary.schedule_sync.async_refresh.await_count >= 1

    entry_site_only.runtime_data = None
    entry_one.runtime_data = None
    entry_two.runtime_data = None
    entry_three.runtime_data = None
    with pytest.raises(ServiceValidationError):
        await svc_start_stream(SimpleNamespace(data={"site_id": "missing"}))
    with pytest.raises(ServiceValidationError):
        await svc_stop_stream(SimpleNamespace(data={"site_id": "missing"}))

    supports_response = registered[(DOMAIN, "trigger_message")]["kwargs"][
        "supports_response"
    ]
    from custom_components.enphase_ev.services import SupportsResponse

    assert supports_response is SupportsResponse.OPTIONAL
    assert fake_service_helper.calls >= 3

    with pytest.raises(ServiceValidationError):
        await svc_request_grid_otp(SimpleNamespace(data={}))
    with pytest.raises(ServiceValidationError):
        await svc_set_grid_mode(
            SimpleNamespace(
                data={
                    "device_id": [charger_one.id, other_site_device.id],
                    "mode": "on_grid",
                    "otp": "1234",
                }
            )
        )
    with pytest.raises(ServiceValidationError):
        await svc_request_grid_otp(SimpleNamespace(data={"site_id": "missing-site"}))


@pytest.mark.asyncio
async def test_device_service_routing_prefers_owning_entry_over_empty_serial_fallback(
    hass: HomeAssistant, monkeypatch
) -> None:
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    monkeypatch.setattr(
        "custom_components.enphase_ev.services.ha_service",
        SimpleNamespace(async_extract_referenced_device_ids=lambda *_args: []),
    )

    serial = "EV-ROUTE-1"
    site_id = "site-route"
    site_only_coord = SimpleNamespace(
        site_id=site_id,
        site_only=True,
        serials=set(),
        data={},
        async_start_charging=AsyncMock(return_value={"status": "wrong"}),
    )
    owning_coord = SimpleNamespace(
        site_id=site_id,
        site_only=False,
        serials={serial},
        data={serial: {}},
        async_start_charging=AsyncMock(return_value={"status": "ok"}),
    )

    site_only_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: site_id},
        title="Site Only",
        unique_id="site-only-entry",
    )
    site_only_entry.add_to_hass(hass)
    site_only_entry.runtime_data = EnphaseRuntimeData(coordinator=site_only_coord)

    owning_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: site_id},
        title="Owning Entry",
        unique_id="owning-entry",
    )
    owning_entry.add_to_hass(hass)
    owning_entry.runtime_data = EnphaseRuntimeData(coordinator=owning_coord)

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owning_entry.entry_id,
        identifiers={(DOMAIN, serial)},
        manufacturer="Enphase",
        name="Routed Charger",
    )

    async_setup_services(hass)
    svc_start = registered[(DOMAIN, "start_charging")]["handler"]

    await svc_start(
        SimpleNamespace(
            data={
                "device_id": device.id,
                "charging_level": 24,
                "connector_id": 1,
            }
        )
    )

    owning_coord.async_start_charging.assert_awaited_once_with(
        serial, requested_amps=24, connector_id=1
    )
    site_only_coord.async_start_charging.assert_not_awaited()


@pytest.mark.asyncio
async def test_device_service_routing_prefers_global_exact_match_before_empty_entry(
    hass: HomeAssistant, monkeypatch
) -> None:
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    monkeypatch.setattr(
        "custom_components.enphase_ev.services.ha_service",
        SimpleNamespace(async_extract_referenced_device_ids=lambda *_args: []),
    )

    serial = "EV-STALE-ENTRY-1"
    empty_coord = SimpleNamespace(
        site_id="empty-site",
        site_only=False,
        serials=set(),
        data={},
        async_start_charging=AsyncMock(return_value={"status": "wrong"}),
    )
    owning_coord = SimpleNamespace(
        site_id="owning-site",
        site_only=False,
        serials={serial},
        data={serial: {}},
        async_start_charging=AsyncMock(return_value={"status": "ok"}),
    )

    empty_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "empty-site"},
        title="Empty Entry",
        unique_id="empty-entry",
    )
    empty_entry.add_to_hass(hass)
    empty_entry.runtime_data = EnphaseRuntimeData(coordinator=empty_coord)

    owning_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "owning-site"},
        title="Owning Entry",
        unique_id="owning-entry",
    )
    owning_entry.add_to_hass(hass)
    owning_entry.runtime_data = EnphaseRuntimeData(coordinator=owning_coord)

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=empty_entry.entry_id,
        identifiers={(DOMAIN, serial)},
        manufacturer="Enphase",
        name="Stale Entry Charger",
    )

    async_setup_services(hass)
    svc_start = registered[(DOMAIN, "start_charging")]["handler"]

    await svc_start(
        SimpleNamespace(
            data={
                "device_id": device.id,
                "charging_level": 32,
                "connector_id": 1,
            }
        )
    )

    owning_coord.async_start_charging.assert_awaited_once_with(
        serial, requested_amps=32, connector_id=1
    )
    empty_coord.async_start_charging.assert_not_awaited()


@pytest.mark.asyncio
async def test_device_service_routing_skips_ambiguous_empty_serial_fallback(
    hass: HomeAssistant, monkeypatch
) -> None:
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    monkeypatch.setattr(
        "custom_components.enphase_ev.services.ha_service",
        SimpleNamespace(async_extract_referenced_device_ids=lambda *_args: []),
    )

    serial = "EV-AMBIGUOUS-EMPTY-1"
    first_empty_coord = SimpleNamespace(
        site_id="first-empty-site",
        site_only=False,
        serials=set(),
        data={},
        async_start_charging=AsyncMock(return_value={"status": "first"}),
    )
    second_empty_coord = SimpleNamespace(
        site_id="second-empty-site",
        site_only=False,
        serials=set(),
        data={},
        async_start_charging=AsyncMock(return_value={"status": "second"}),
    )

    first_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "first-empty-site"},
        title="First Empty Entry",
        unique_id="first-empty-entry",
    )
    first_entry.add_to_hass(hass)
    first_entry.runtime_data = EnphaseRuntimeData(coordinator=first_empty_coord)

    second_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "second-empty-site"},
        title="Second Empty Entry",
        unique_id="second-empty-entry",
    )
    second_entry.add_to_hass(hass)
    second_entry.runtime_data = EnphaseRuntimeData(coordinator=second_empty_coord)

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=first_entry.entry_id,
        identifiers={(DOMAIN, serial)},
        manufacturer="Enphase",
        name="Ambiguous Empty Charger",
    )

    async_setup_services(hass)
    svc_start = registered[(DOMAIN, "start_charging")]["handler"]

    with pytest.raises(ServiceValidationError):
        await svc_start(
            SimpleNamespace(
                data={
                    "device_id": device.id,
                    "charging_level": 16,
                    "connector_id": 1,
                }
            )
        )

    first_empty_coord.async_start_charging.assert_not_awaited()
    second_empty_coord.async_start_charging.assert_not_awaited()


@pytest.mark.asyncio
async def test_device_service_routing_allows_single_empty_serial_fallback(
    hass: HomeAssistant, monkeypatch
) -> None:
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    monkeypatch.setattr(
        "custom_components.enphase_ev.services.ha_service",
        SimpleNamespace(async_extract_referenced_device_ids=lambda *_args: []),
    )

    serial = "EV-SINGLE-EMPTY-1"
    empty_coord = SimpleNamespace(
        site_id="single-empty-site",
        site_only=False,
        serials=set(),
        data={},
        async_start_charging=AsyncMock(return_value={"status": "fallback"}),
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "single-empty-site"},
        title="Single Empty Entry",
        unique_id="single-empty-entry",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = EnphaseRuntimeData(coordinator=empty_coord)

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, serial)},
        manufacturer="Enphase",
        name="Single Empty Charger",
    )

    async_setup_services(hass)
    svc_start = registered[(DOMAIN, "start_charging")]["handler"]

    await svc_start(
        SimpleNamespace(
            data={
                "device_id": device.id,
                "charging_level": 12,
                "connector_id": 1,
            }
        )
    )

    empty_coord.async_start_charging.assert_awaited_once_with(
        serial, requested_amps=12, connector_id=1
    )


@pytest.mark.asyncio
async def test_device_service_routing_helper_guard_paths(
    hass: HomeAssistant, monkeypatch
) -> None:
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    def extract_helper(func, target):
        for cell in func.__closure__ or ():
            value = cell.cell_contents
            if callable(value) and getattr(value, "__name__", "") == target:
                return value
        raise AssertionError(f"helper {target} not found")

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)
    monkeypatch.setattr(
        "custom_components.enphase_ev.services.ha_service",
        SimpleNamespace(async_extract_referenced_device_ids=lambda *_args: []),
    )

    async_setup_services(hass)
    svc_start = registered[(DOMAIN, "start_charging")]["handler"]
    resolve_targets = extract_helper(svc_start, "_resolve_charger_targets")
    resolve_context = extract_helper(resolve_targets, "_resolve_device_routing_context")
    get_coord = extract_helper(resolve_targets, "_get_coordinator_for_sn")
    config_ids_for_device = extract_helper(
        resolve_context, "_config_entry_ids_for_device"
    )
    can_fallback = extract_helper(get_coord, "_coordinator_can_fallback_for_serial")

    assert not can_fallback(
        SimpleNamespace(site_id="other-site", site_only=False, serials=set(), data={}),
        "EV-GUARD",
        "site-guard",
    )
    assert not can_fallback(
        SimpleNamespace(site_id="site-guard", site_only=True, serials=set(), data={}),
        "EV-GUARD",
        "site-guard",
    )
    assert config_ids_for_device(
        SimpleNamespace(async_get=Mock()),
        SimpleNamespace(
            config_entries=None, config_entry_id="legacy-entry", via_device_id=None
        ),
    ) == ["legacy-entry"]
    assert (
        config_ids_for_device(
            SimpleNamespace(async_get=Mock()),
            SimpleNamespace(
                config_entries=None, config_entry_id=None, via_device_id=None
            ),
        )
        == []
    )
    assert (
        config_ids_for_device(
            SimpleNamespace(async_get=Mock(return_value=None)),
            SimpleNamespace(
                config_entries=None, config_entry_id=None, via_device_id="missing"
            ),
        )
        == []
    )
    parent_device = SimpleNamespace(
        config_entries={"parent-entry"}, config_entry_id=None, via_device_id=None
    )
    assert config_ids_for_device(
        SimpleNamespace(async_get=Mock(return_value=parent_device)),
        SimpleNamespace(
            config_entries=None, config_entry_id=None, via_device_id="parent"
        ),
    ) == ["parent-entry"]

    site_serial = "EV-SITE-DUPE"
    site_dup_one = SimpleNamespace(
        site_id="site-dupe",
        site_only=False,
        serials={site_serial},
        data={},
    )
    site_dup_two = SimpleNamespace(
        site_id="site-dupe",
        site_only=False,
        serials={site_serial},
        data={},
    )
    for index, coord in enumerate((site_dup_one, site_dup_two), start=1):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_SITE_ID: "site-dupe"},
            title=f"Site Duplicate {index}",
            unique_id=f"site-duplicate-{index}",
        )
        entry.add_to_hass(hass)
        entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    assert await get_coord(site_serial, site_id="site-dupe") is None

    for index in range(2):
        coord = SimpleNamespace(
            site_id="site-empty-dupe",
            site_only=False,
            serials=set(),
            data={},
        )
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_SITE_ID: "site-empty-dupe"},
            title=f"Site Empty Duplicate {index}",
            unique_id=f"site-empty-duplicate-{index}",
        )
        entry.add_to_hass(hass)
        entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    assert await get_coord("EV-SITE-EMPTY-DUPE", site_id="site-empty-dupe") is None

    global_serial = "EV-GLOBAL-DUPE"
    global_dup_one = SimpleNamespace(
        site_id="global-dupe-one",
        site_only=False,
        serials={global_serial},
        data={},
    )
    global_dup_two = SimpleNamespace(
        site_id="global-dupe-two",
        site_only=False,
        serials={global_serial},
        data={},
    )
    for index, coord in enumerate((global_dup_one, global_dup_two), start=1):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_SITE_ID: coord.site_id},
            title=f"Global Duplicate {index}",
            unique_id=f"global-duplicate-{index}",
        )
        entry.add_to_hass(hass)
        entry.runtime_data = EnphaseRuntimeData(coordinator=coord)

    assert await get_coord(global_serial) is None

    site_fallback_coord = SimpleNamespace(
        site_id="site-fallback",
        site_only=False,
        serials=set(),
        data={},
    )
    site_fallback_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "site-fallback"},
        title="Site Fallback",
        unique_id="site-fallback-entry",
    )
    site_fallback_entry.add_to_hass(hass)
    site_fallback_entry.runtime_data = EnphaseRuntimeData(
        coordinator=site_fallback_coord
    )

    assert (
        await get_coord("EV-SITE-FALLBACK", site_id="site-fallback")
        is site_fallback_coord
    )


def test_init_module_importable() -> None:
    import importlib

    module = importlib.import_module("custom_components.enphase_ev.__init__")
    assert module.DOMAIN == DOMAIN


@pytest.mark.asyncio
async def test_service_helper_resolve_functions_cover_none_branches(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    """Ensure resolve helpers handle missing identifiers gracefully."""
    registered: dict[tuple[str, str], dict[str, object]] = {}

    def fake_register(self, domain, service, handler, schema=None, **kwargs):
        registered[(domain, service)] = {
            "handler": handler,
            "schema": schema,
            "kwargs": kwargs,
        }

    monkeypatch.setattr(hass.services.__class__, "async_register", fake_register)

    async_setup_services(hass)

    svc_start = registered[(DOMAIN, "start_charging")]["handler"]
    svc_stop = registered[(DOMAIN, "stop_charging")]["handler"]
    svc_clear = registered[(DOMAIN, "clear_reauth_issue")]["handler"]

    def _extract_helper(func, target):
        for cell in func.__closure__ or ():
            value = cell.cell_contents
            if callable(value) and getattr(value, "__name__", "") == target:
                return value
        raise AssertionError(f"helper {target} not found")

    resolve_targets = _extract_helper(svc_start, "_resolve_charger_targets")
    resolve_device_routing_context = _extract_helper(
        resolve_targets, "_resolve_device_routing_context"
    )
    resolve_site = _extract_helper(svc_clear, "_resolve_site_id")

    dev_reg = dr.async_get(hass)
    assert await resolve_device_routing_context("does-not-exist") is None
    assert await resolve_site("does-not-exist") is None

    site_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "site:ABC123")},
        manufacturer="Enphase",
        name="Site Device",
    )
    assert await resolve_device_routing_context(site_device.id) is None
    assert await resolve_site(site_device.id) == "ABC123"

    child_no_parent = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("other", "value")},
        manufacturer="Vendor",
        name="Third Party Device",
    )
    assert await resolve_device_routing_context(child_no_parent.id) is None
    assert await resolve_site(child_no_parent.id) is None

    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "site:PARENT")},
        manufacturer="Enphase",
        name="Parent Site",
    )
    child_with_via = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "EVCHILD")},
        manufacturer="Enphase",
        name="Child Device",
        via_device=(DOMAIN, "site:PARENT"),
    )
    assert await resolve_device_routing_context(child_with_via.id) == (
        "EVCHILD",
        "PARENT",
        [config_entry.entry_id],
    )
    assert await resolve_site(child_with_via.id) == "PARENT"

    type_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "type:TYPED:envoy")},
        manufacturer="Enphase",
        name="Gateway (1)",
    )
    assert await resolve_device_routing_context(type_device.id) is None
    assert await resolve_site(type_device.id) == "TYPED"

    child_with_type_parent = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "EVTYPED")},
        manufacturer="Enphase",
        name="Typed Child",
        via_device=(DOMAIN, "type:TYPED:envoy"),
    )
    assert await resolve_site(child_with_type_parent.id) == "TYPED"

    from custom_components.enphase_ev.coordinator import ServiceValidationError

    with pytest.raises(ServiceValidationError):
        await svc_stop(SimpleNamespace(data={}))


def test_init_module_reload_executes_module_code() -> None:
    module = importlib.import_module("custom_components.enphase_ev")
    assert importlib.reload(module).DOMAIN == DOMAIN


class _FakeDevice(SimpleNamespace):
    pass


class _FakeDeviceRegistry:
    def __init__(self) -> None:
        self._devices: dict[tuple[str, str], _FakeDevice] = {}
        self._next_id = 1

    def async_get_device(self, *, identifiers):
        ident = next(iter(identifiers))
        return self._devices.get(ident)

    def async_get_or_create(self, **kwargs):
        ident = next(iter(kwargs["identifiers"]))
        existing = self._devices.get(ident)
        if existing is None:
            existing = _FakeDevice(
                id=f"dev-{self._next_id}",
                identifiers={ident},
                manufacturer=None,
                name=None,
                model=None,
                model_id=None,
                serial_number=None,
                hw_version=None,
                sw_version=None,
                via_device_id=None,
            )
            self._next_id += 1
            self._devices[ident] = existing
        for field in (
            "name",
            "manufacturer",
            "model",
            "model_id",
            "serial_number",
            "hw_version",
            "sw_version",
        ):
            if field in kwargs:
                setattr(existing, field, kwargs[field])
        if "via_device" in kwargs:
            via = kwargs.get("via_device")
            if via is None:
                existing.via_device_id = None
            else:
                parent = self._devices.get(via)
                existing.via_device_id = parent.id if parent else None
        return existing

    def async_remove_device(self, device_id):
        for ident, device in list(self._devices.items()):
            if device.id == device_id:
                del self._devices[ident]
                break


def test_sync_type_devices_skips_invalid_and_updates_existing(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    existing = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="LegacyVendor",
        name="Old Gateway",
        model="Old",
    )

    coord = _with_inventory_view(
        SimpleNamespace(
            iter_type_keys=lambda: ["invalid", "empty", "envoy"],
            type_identifier=lambda key: (
                None if key == "invalid" else (DOMAIN, f"type:{site_id}:{key}")
            ),
            type_label=lambda key: "" if key == "empty" else "Gateway",
            type_device_name=lambda key: "" if key == "empty" else "Gateway (1)",
        )
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)

    assert "envoy" in type_devices
    updated = type_devices["envoy"]
    assert updated.id == existing.id
    assert updated.manufacturer == "Enphase"
    assert updated.name == "Gateway (1)"
    assert updated.model == "Gateway"


def test_sync_registry_devices_runs_cleanup_when_requested(
    config_entry, monkeypatch
) -> None:
    coord = SimpleNamespace()
    dev_reg = SimpleNamespace()
    hass = SimpleNamespace()
    type_devices = {"envoy": object()}
    sync_types = Mock(return_value=type_devices)
    sync_chargers = Mock()
    prune = Mock()
    remove_empty = Mock()
    monkeypatch.setattr(enphase_init, "_sync_type_devices", sync_types)
    monkeypatch.setattr(enphase_init, "_sync_charger_devices", sync_chargers)
    monkeypatch.setattr(enphase_init, "_prune_inactive_serial_entities", prune)
    monkeypatch.setattr(
        enphase_init, "_remove_empty_inactive_serial_devices", remove_empty
    )

    _sync_registry_devices(
        config_entry,
        coord,
        dev_reg,
        "site",
        hass=hass,
    )

    sync_types.assert_called_once_with(config_entry, coord, dev_reg, "site")
    sync_chargers.assert_called_once_with(
        config_entry, coord, dev_reg, "site", type_devices
    )
    prune.assert_called_once_with(hass, config_entry, coord, "site")
    remove_empty.assert_called_once_with(hass, config_entry, coord, dev_reg, "site")


def test_sync_type_devices_deduplicates_merged_identifiers(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    coord = _with_inventory_view(
        SimpleNamespace(
            iter_type_keys=lambda: ["envoy", "meter", "enpower"],
            type_identifier=lambda _key: (DOMAIN, f"type:{site_id}:envoy"),
            type_label=lambda _key: "Gateway",
            type_device_name=lambda _key: "Gateway (1)",
        )
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)

    assert set(type_devices) == {"envoy", "meter", "enpower"}
    assert len({type_devices[key].id for key in type_devices}) == 1
    assert len(dev_reg._devices) == 1


def test_sync_type_devices_uses_model_and_hw_summary(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()

    coord = _with_inventory_view(
        SimpleNamespace(
            iter_type_keys=lambda: ["microinverter"],
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
            type_label=lambda key: "Microinverters",
            type_device_name=lambda key: "Microinverters (16)",
            type_device_model=lambda key: "IQ7A x16",
            type_device_serial_number=lambda key: "INV-1 x16",
            type_device_model_id=lambda key: "IQ7A-72-2-US x16",
            type_device_sw_version=lambda key: "520-00082-r01-v04.30.32 x16",
            type_device_hw_version=lambda key: "IQ7A-72-2-US x16",
        )
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)
    device = type_devices["microinverter"]
    assert device.model == "IQ7A x16"
    assert device.serial_number == "INV-1 x16"
    assert device.model_id == "IQ7A-72-2-US x16"
    assert device.sw_version == "520-00082-r01-v04.30.32 x16"
    assert device.hw_version == "IQ7A-72-2-US x16"


def test_sync_type_devices_uses_inventory_view_firmware_summaries(
    config_entry,
) -> None:
    from custom_components.enphase_ev.inventory_view import InventoryView

    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    heatpump_member = {
        "device_type": "HEAT_PUMP",
        "device_uid": "HP-1",
        "name": "Heat Pump",
        "firmware-version": "3.0",
        "part-number": "PN-1",
    }
    coord = SimpleNamespace(
        site_id=site_id,
        _selected_type_keys=None,
        _type_device_order=["encharge", "microinverter", "heatpump"],
        _type_device_buckets={
            "encharge": {
                "type_key": "encharge",
                "type_label": "Battery",
                "count": 3,
                "devices": [
                    {"serial_number": "BAT-1", "sw_version": "1.0"},
                    {"serial_number": "BAT-2", "sw_version": "1.0"},
                    {"serial_number": "BAT-3", "sw_version": "2.0"},
                ],
            },
            "microinverter": {
                "type_key": "microinverter",
                "type_label": "Microinverters",
                "count": 3,
                "devices": [
                    {"serial_number": "INV-1", "fw1": "4.0"},
                    {"serial_number": "INV-2", "fw1": "4.0"},
                    {"serial_number": "INV-3", "fw2": "5.0"},
                ],
            },
            "heatpump": {
                "type_key": "heatpump",
                "type_label": "Heat Pump",
                "count": 1,
                "devices": [heatpump_member],
            },
        },
        heatpump_runtime=SimpleNamespace(
            _heatpump_primary_member=lambda: heatpump_member
        ),
        inventory_runtime=SimpleNamespace(),
    )
    coord.inventory_view = InventoryView(coord)

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)

    assert type_devices["encharge"].sw_version == "1.0 x2, 2.0 x1"
    assert type_devices["microinverter"].sw_version == "4.0 x2, 5.0 x1"
    assert type_devices["heatpump"].sw_version == "3.0"


def test_inventory_type_device_sw_version_for_registry_fallbacks() -> None:
    assert (
        _inventory_type_device_sw_version_for_registry(SimpleNamespace(), "envoy")
        is None
    )
    assert (
        _inventory_type_device_sw_version_for_registry(
            SimpleNamespace(type_device_sw_version=lambda _key: "7.0"), "envoy"
        )
        == "7.0"
    )
    assert (
        _inventory_type_device_sw_version_for_registry(
            SimpleNamespace(
                type_device_sw_version_summary=lambda _key: " ",
                type_device_sw_version=lambda _key: "8.0",
            ),
            "envoy",
        )
        == "8.0"
    )
    assert (
        _inventory_type_device_sw_version_for_registry(
            SimpleNamespace(type_device_sw_version_summary=lambda _key: " "),
            "envoy",
        )
        is None
    )


def test_sync_type_devices_updates_existing_hw_summary(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:microinverter")},
        manufacturer="Enphase",
        name="Microinverters (16)",
        model="IQ7A x16",
        hw_version="Normal 15 | Warning 1 | Error 0 | Not Reporting 0",
    )

    coord = _with_inventory_view(
        SimpleNamespace(
            iter_type_keys=lambda: ["microinverter"],
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
            type_label=lambda key: "Microinverters",
            type_device_name=lambda key: "Microinverters (16)",
            type_device_model=lambda key: "IQ7A x16",
            type_device_hw_version=lambda key: "IQ7A-72-2-US x16",
        )
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)
    device = type_devices["microinverter"]
    assert device.hw_version == "IQ7A-72-2-US x16"


def test_sync_type_devices_updates_existing_serial_model_id_and_sw(
    config_entry,
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="Gateway",
        model="Gateway",
        serial_number="old",
        model_id="old",
        sw_version="1.0",
    )

    coord = _with_inventory_view(
        SimpleNamespace(
            iter_type_keys=lambda: ["envoy"],
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
            type_label=lambda key: "Gateway",
            type_device_name=lambda key: "IQ System Controller 3 INT",
            type_device_model=lambda key: "IQ System Controller 3 INT",
            type_device_serial_number=lambda key: "Controller: NEW-SN",
            type_device_model_id=lambda key: "NEW-SKU x1",
            type_device_sw_version=lambda key: "9.0.0 x1",
        )
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)
    device = type_devices["envoy"]
    assert device.name == "IQ System Controller 3 INT"
    assert device.model == "IQ System Controller 3 INT"
    assert device.serial_number == "Controller: NEW-SN"
    assert device.model_id == "NEW-SKU x1"
    assert device.sw_version == "9.0.0 x1"


def test_sync_type_devices_omits_redundant_model_id(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()

    coord = _with_inventory_view(
        SimpleNamespace(
            iter_type_keys=lambda: ["iqevse", "encharge"],
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
            type_label=lambda key: "EV Charger" if key == "iqevse" else "Battery",
            type_device_name=lambda key: (
                "IQ EV Charger" if key == "iqevse" else "IQ Battery"
            ),
            type_device_model=lambda key: (
                "IQ EV Charger (IQ-EVSE-EU-3032)"
                if key == "iqevse"
                else "B05-T02-ROW00-1-2"
            ),
            type_device_model_id=lambda key: (
                "IQ-EVSE-EU-3032-0105-1300" if key == "iqevse" else "B05-T02-ROW00-1-2"
            ),
        )
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)
    assert type_devices["encharge"].model_id is None


def test_sync_type_devices_clears_stale_metadata_when_helpers_return_none(
    config_entry,
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="Gateway",
        model="Gateway",
        serial_number="old-sn",
        model_id="old-sku",
        sw_version="old-sw",
        hw_version="old-hw",
    )

    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    coord = _with_inventory_view(
        SimpleNamespace(
            iter_type_keys=lambda: ["envoy"],
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
            type_label=lambda _key: "Gateway",
            type_device_name=lambda _key: "Gateway",
            type_device_model=lambda _key: "Gateway",
            type_device_serial_number=lambda _key: _BadStr(),
            type_device_model_id=lambda _key: _BadStr(),
            type_device_sw_version=lambda _key: "   ",
            type_device_hw_version=lambda _key: None,
        )
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)
    device = type_devices["envoy"]
    assert device.serial_number is None
    assert device.model_id is None
    assert device.sw_version is None
    assert device.hw_version is None


def test_sync_type_devices_clears_metadata_when_inventory_view_returns_none(
    config_entry,
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="Gateway",
        model="Gateway",
        serial_number="kept-sn",
        model_id="kept-sku",
        sw_version="kept-sw",
        hw_version="kept-hw",
    )

    coord = _with_inventory_view(
        SimpleNamespace(
            iter_type_keys=lambda: ["envoy"],
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
            type_label=lambda _key: "Gateway",
            type_device_name=lambda _key: "Gateway",
            type_device_model=lambda _key: "Gateway",
        )
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, site_id)
    device = type_devices["envoy"]
    assert device.serial_number is None
    assert device.model_id is None
    assert device.sw_version is None
    assert device.hw_version is None


def test_sync_charger_devices_clears_legacy_type_parent(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:iqevse")},
        manufacturer="Enphase",
        name="EV Chargers (1)",
        model="EV Chargers",
    )

    coord = _with_inventory_view(
        SimpleNamespace(
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
            iter_serials=lambda: [RANDOM_SERIAL],
            data={
                RANDOM_SERIAL: {
                    "display_name": "Garage Charger",
                    "model_name": "IQ EVSE",
                    "hw_version": "1.0",
                    "sw_version": "2.0",
                }
            },
        )
    )

    _sync_charger_devices(config_entry, coord, dev_reg, site_id, type_devices={})
    charger = dev_reg.async_get_device(identifiers={(DOMAIN, RANDOM_SERIAL)})
    assert charger is not None
    assert charger.via_device_id is None


def test_sync_charger_devices_marks_existing_legacy_parent_for_update(
    config_entry,
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:iqevse")},
        manufacturer="Enphase",
        name="EV Chargers (1)",
        model="EV Chargers",
    )
    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL)},
        manufacturer="Enphase",
        name="Garage Charger",
        via_device=(DOMAIN, f"type:{site_id}:iqevse"),
    )

    coord = _with_inventory_view(
        SimpleNamespace(
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
            iter_serials=lambda: [RANDOM_SERIAL],
            data={RANDOM_SERIAL: {"display_name": "Garage Charger"}},
        )
    )

    _sync_charger_devices(config_entry, coord, dev_reg, site_id, type_devices={})

    charger = dev_reg.async_get_device(identifiers={(DOMAIN, RANDOM_SERIAL)})
    assert charger is not None
    assert charger.via_device_id is None


@pytest.mark.asyncio
async def test_startup_migration_removes_evse_type_device_and_inventory_entity(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    gateway = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="IQ Gateway",
        model="IQ Gateway",
    )
    evse_type = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:iqevse")},
        manufacturer="Enphase",
        name="EV Chargers (1)",
        model="EV Chargers",
    )
    charger = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL)},
        manufacturer="Enphase",
        name="Garage Charger",
        via_device=(DOMAIN, f"type:{site_id}:iqevse"),
    )
    entity = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_site_{site_id}_type_iqevse_inventory",
        suggested_object_id=f"site_{site_id}_ev_chargers_inventory",
        config_entry=config_entry,
        device_id=evse_type.id,
    )

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id

        def startup_migrations_ready(self) -> bool:
            return True

    runtime_data = EnphaseRuntimeData(
        coordinator=DummyCoordinator(),
        firmware_catalog=None,
        evse_firmware_details=None,
    )
    config_entry.runtime_data = runtime_data
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_cloud_entity_unique_ids",
        Mock(),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_legacy_gateway_type_devices",
        Mock(),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_orphaned_update_entities_to_type_devices",
        Mock(),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_cloud_entities_to_cloud_device",
        Mock(),
    )

    _complete_startup_migrations_if_ready(
        hass,
        config_entry,
        _with_inventory_view(DummyCoordinator()),
        dev_reg,
        site_id,
    )

    assert dev_reg.async_get(gateway.id) is not None
    assert dev_reg.async_get(evse_type.id) is None
    assert ent_reg.async_get(entity.entity_id) is None
    migrated_charger = dev_reg.async_get(charger.id)
    assert migrated_charger is not None


def test_sync_charger_devices_dedupes_extended_evse_model_display(config_entry) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = _FakeDeviceRegistry()
    dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:iqevse")},
        manufacturer="Enphase",
        name="EV Chargers (1)",
        model="EV Chargers",
    )

    coord = _with_inventory_view(
        SimpleNamespace(
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
            iter_serials=lambda: [RANDOM_SERIAL],
            data={
                RANDOM_SERIAL: {
                    "display_name": "IQ EV Charger (IQ-EVSE-EU-3032)",
                    "model_name": "IQ-EVSE-EU-3032-0105-1300",
                }
            },
        )
    )

    _sync_charger_devices(config_entry, coord, dev_reg, site_id, type_devices={})
    charger = dev_reg.async_get_device(identifiers={(DOMAIN, RANDOM_SERIAL)})
    assert charger is not None
    assert charger.name == "IQ EV Charger (IQ-EVSE-EU-3032)"
    assert charger.model == "IQ EV Charger (IQ-EVSE-EU-3032)"


def test_remove_evse_type_device_and_entities_handles_guard_paths(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    module = importlib.import_module("custom_components.enphase_ev")

    original_er = module.er
    monkeypatch.setattr(module, "er", None)
    _remove_evse_type_device_and_entities(
        hass,
        config_entry,
        SimpleNamespace(async_get_device=lambda **_kwargs: None),
        config_entry.data[CONF_SITE_ID],
    )
    monkeypatch.setattr(module, "er", original_er)

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    _remove_evse_type_device_and_entities(
        hass,
        config_entry,
        SimpleNamespace(async_get_device=lambda **_kwargs: None),
        BadStr(),
    )
    _remove_evse_type_device_and_entities(
        hass,
        config_entry,
        SimpleNamespace(async_get_device=lambda **_kwargs: None),
        "   ",
    )
    _remove_evse_type_device_and_entities(
        hass,
        config_entry,
        SimpleNamespace(async_get_device=lambda **_kwargs: None),
        config_entry.data[CONF_SITE_ID],
    )
    _remove_evse_type_device_and_entities(
        hass,
        config_entry,
        SimpleNamespace(
            async_get_device=lambda **_kwargs: SimpleNamespace(id=None),
        ),
        config_entry.data[CONF_SITE_ID],
    )

    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    _remove_evse_type_device_and_entities(
        hass,
        config_entry,
        SimpleNamespace(
            async_get_device=lambda **_kwargs: SimpleNamespace(id="evse-device"),
        ),
        config_entry.data[CONF_SITE_ID],
    )


def test_remove_evse_type_device_and_entities_handles_remove_failures(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    owned_no_entity = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id=config_entry.entry_id,
        entity_id=None,
    )
    foreign_entity = SimpleNamespace(
        platform="other_domain",
        config_entry_id=config_entry.entry_id,
        entity_id="sensor.foreign",
    )
    failing_entity = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id=config_entry.entry_id,
        entity_id="sensor.ev_inventory",
    )

    ent_reg = SimpleNamespace(
        async_remove=lambda entity_id: (_ for _ in ()).throw(RuntimeError(entity_id)),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: ent_reg,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_entries_for_device",
        lambda _reg, _device_id: [owned_no_entity, foreign_entity, failing_entity],
    )

    dev_reg = SimpleNamespace(
        async_get_device=lambda **kwargs: (
            SimpleNamespace(id="evse-device")
            if next(iter(kwargs["identifiers"])) == (DOMAIN, f"type:{site_id}:iqevse")
            else None
        ),
        async_remove_device=lambda _device_id: (_ for _ in ()).throw(
            RuntimeError("remove failed")
        ),
    )

    _remove_evse_type_device_and_entities(hass, config_entry, dev_reg, site_id)


def test_remove_evse_type_device_and_entities_removes_device_when_entries_cleared(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    owned_entity = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id=config_entry.entry_id,
        entity_id="sensor.ev_inventory",
    )
    entry_lists = [[owned_entity], []]

    ent_reg = SimpleNamespace(async_remove=lambda _entity_id: None)
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: ent_reg,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_entries_for_device",
        lambda _reg, _device_id: entry_lists.pop(0),
    )
    removed_device_ids: list[str] = []
    dev_reg = SimpleNamespace(
        async_get_device=lambda **kwargs: (
            SimpleNamespace(id="evse-device")
            if next(iter(kwargs["identifiers"])) == (DOMAIN, f"type:{site_id}:iqevse")
            else None
        ),
        async_remove_device=lambda device_id: removed_device_ids.append(device_id),
    )

    _remove_evse_type_device_and_entities(hass, config_entry, dev_reg, site_id)

    assert removed_device_ids == ["evse-device"]


def test_remove_evse_type_device_and_entities_handles_remove_device_failure(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    owned_entity = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id=config_entry.entry_id,
        entity_id="sensor.ev_inventory",
    )
    entry_lists = [[owned_entity], []]

    ent_reg = SimpleNamespace(async_remove=lambda _entity_id: None)
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: ent_reg,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_entries_for_device",
        lambda _reg, _device_id: entry_lists.pop(0),
    )
    dev_reg = SimpleNamespace(
        async_get_device=lambda **kwargs: (
            SimpleNamespace(id="evse-device")
            if next(iter(kwargs["identifiers"])) == (DOMAIN, f"type:{site_id}:iqevse")
            else None
        ),
        async_remove_device=lambda _device_id: (_ for _ in ()).throw(
            RuntimeError("remove failed")
        ),
    )

    _remove_evse_type_device_and_entities(hass, config_entry, dev_reg, site_id)


def test_evse_model_helpers_cover_error_and_empty_paths() -> None:
    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("boom")

    assert _normalize_evse_model_name(_BadStr()) is None
    assert _normalize_evse_model_name("   ") is None
    assert _normalize_evse_model_name("IQ-EVSE-EU-3032-0105-1300") == "IQ-EVSE-EU-3032"
    assert (
        _normalize_evse_model_name("iq-evse-na1-4040-0105-1300") == "IQ-EVSE-NA1-4040"
    )
    assert _normalize_evse_model_name("IQ-EVSE-EU") == "IQ-EVSE-EU"
    assert _compose_charger_model_display(None, _BadStr(), "   ") is None


def test_iter_entity_registry_entries_handles_edge_shapes() -> None:
    assert _iter_entity_registry_entries(SimpleNamespace()) == []

    class _ValuesRaises:
        def values(self):
            raise RuntimeError("boom")

    class _DictNoCallableValues(dict):
        values = []  # type: ignore[assignment]

    assert (
        _iter_entity_registry_entries(SimpleNamespace(entities=_ValuesRaises())) == []
    )
    assert _iter_entity_registry_entries(SimpleNamespace(entities={"x": 1})) == [1]
    assert _iter_entity_registry_entries(
        SimpleNamespace(entities=_DictNoCallableValues(x=1))
    ) == [1]
    assert _iter_entity_registry_entries(SimpleNamespace(entities=["bad"])) == []


def test_entries_for_device_falls_back_when_helper_errors(monkeypatch) -> None:
    reg_entries = {
        "sensor.a": SimpleNamespace(device_id="dev-1"),
        "sensor.b": SimpleNamespace(device_id="dev-2"),
    }
    ent_reg = SimpleNamespace(entities=reg_entries)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_entries_for_device", _boom
    )
    entries = _entries_for_device(ent_reg, "dev-1")
    assert len(entries) == 1
    assert entries[0].device_id == "dev-1"


def test_find_entity_id_by_unique_id_fallback_scan_paths() -> None:
    entries = {
        "sensor.keep": SimpleNamespace(
            unique_id="enphase_ev_site_SITE-1_latency_ms",
            entity_id="sensor.keep",
            platform=DOMAIN,
            config_entry_id="entry-1",
            domain=None,
        ),
        "sensor.foreign": SimpleNamespace(
            unique_id="enphase_ev_site_SITE-1_latency_ms",
            entity_id="sensor.foreign",
            platform=DOMAIN,
            config_entry_id="entry-2",
            domain=None,
        ),
    }
    ent_reg = SimpleNamespace(entities=entries)

    found = _find_entity_id_by_unique_id(
        ent_reg,
        "sensor",
        "enphase_ev_site_SITE-1_latency_ms",
        entry_id="entry-1",
    )
    assert found == "sensor.keep"

    assert (
        _find_entity_id_by_unique_id(
            ent_reg,
            "binary_sensor",
            "enphase_ev_site_SITE-1_latency_ms",
            entry_id="entry-1",
        )
        is None
    )


def test_find_entity_id_by_unique_id_helper_error_and_unowned_paths() -> None:
    entries = {
        "sensor.mismatch": SimpleNamespace(
            unique_id="enphase_ev_site_SITE-9_other",
            entity_id="sensor.mismatch",
            platform=DOMAIN,
            config_entry_id="entry-1",
            domain="sensor",
        ),
        "sensor.foreign": SimpleNamespace(
            unique_id="enphase_ev_site_SITE-9_latency_ms",
            entity_id="sensor.foreign",
            platform=DOMAIN,
            config_entry_id="entry-2",
            domain="sensor",
        ),
        "sensor.owned": SimpleNamespace(
            unique_id="enphase_ev_site_SITE-9_latency_ms",
            entity_id="sensor.owned",
            platform=DOMAIN,
            config_entry_id="entry-1",
            domain="sensor",
        ),
    }
    ent_reg = SimpleNamespace(
        entities=entries,
        async_get_entity_id=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
    )
    assert (
        _find_entity_id_by_unique_id(
            ent_reg,
            "sensor",
            "enphase_ev_site_SITE-9_latency_ms",
            entry_id="entry-1",
        )
        == "sensor.owned"
    )

    ent_reg_owned_check = SimpleNamespace(
        async_get_entity_id=lambda *_args, **_kwargs: "sensor.foreign",
        async_get=lambda _entity_id: SimpleNamespace(
            platform=DOMAIN,
            config_entry_id="entry-2",
        ),
    )
    assert (
        _find_entity_id_by_unique_id(
            ent_reg_owned_check,
            "sensor",
            "enphase_ev_site_SITE-9_latency_ms",
            entry_id="entry-1",
        )
        is None
    )


def test_is_owned_entity_checks_platform_and_config_entry() -> None:
    assert _is_owned_entity(SimpleNamespace(platform=DOMAIN, config_entry_id="a"), "a")
    assert not _is_owned_entity(
        SimpleNamespace(platform="other", config_entry_id="a"), "a"
    )
    assert not _is_owned_entity(
        SimpleNamespace(platform=DOMAIN, config_entry_id="b"), "a"
    )


def test_remove_legacy_inventory_entities_handles_missing_entity_and_remove_errors() -> (
    None
):
    site_id = "SITE-123"
    attempted: list[str] = []

    def _remove(entity_id: str) -> None:
        attempted.append(entity_id)
        raise RuntimeError("boom")

    ent_reg = SimpleNamespace(
        entities={
            "sensor.missing_id": SimpleNamespace(
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id=f"{DOMAIN}_site_{site_id}_type_meter_inventory",
                entity_id=None,
            ),
            "sensor.remove_error": SimpleNamespace(
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id=f"{DOMAIN}_site_{site_id}_type_envoy_inventory",
                entity_id="sensor.remove_error",
            ),
            "sensor.remove_micro_error": SimpleNamespace(
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id=f"{DOMAIN}_site_{site_id}_type_microinverter_inventory",
                entity_id="sensor.remove_micro_error",
            ),
        },
        async_remove=_remove,
    )

    removed = _remove_legacy_inventory_entities(ent_reg, site_id, entry_id="entry-1")
    assert removed == 0
    assert set(attempted) == {"sensor.remove_error", "sensor.remove_micro_error"}


@pytest.mark.asyncio
async def test_migrate_legacy_gateway_type_devices_rehomes_entities_and_prunes(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    gateway = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="Gateway (3)",
    )
    meter = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:meter")},
        manufacturer="Enphase",
        name="Meter (1)",
    )
    enpower = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:enpower")},
        manufacturer="Enphase",
        name="System Controller (1)",
    )
    dry_contact = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:nc1")},
        manufacturer="Enphase",
        name="NC1",
    )
    site_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="Enphase",
        name=f"Enphase Site {site_id}",
    )

    meter_inventory = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_type_meter_inventory",
        device_id=meter.id,
        config_entry=config_entry,
    )
    gateway_inventory = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_type_envoy_inventory",
        device_id=gateway.id,
        config_entry=config_entry,
    )
    enpower_inventory = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_type_enpower_inventory",
        device_id=enpower.id,
        config_entry=config_entry,
    )
    dry_contact_inventory = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_type_nc1_inventory",
        device_id=dry_contact.id,
        config_entry=config_entry,
    )
    microinverter_inventory = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_type_microinverter_inventory",
        device_id=gateway.id,
        config_entry=config_entry,
    )
    legacy_metric = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_legacy_metric",
        device_id=enpower.id,
        config_entry=config_entry,
    )
    site_metric = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_legacy_site_metric",
        device_id=site_device.id,
        config_entry=config_entry,
    )

    coord = _with_inventory_view(
        SimpleNamespace(
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
        )
    )

    _migrate_legacy_gateway_type_devices(hass, config_entry, coord, dev_reg, site_id)

    assert ent_reg.async_get(meter_inventory.entity_id) is None
    assert ent_reg.async_get(gateway_inventory.entity_id) is None
    assert ent_reg.async_get(microinverter_inventory.entity_id) is None
    moved_enpower = ent_reg.async_get(enpower_inventory.entity_id)
    assert moved_enpower is not None
    assert moved_enpower.device_id == gateway.id
    moved_dry_contact = ent_reg.async_get(dry_contact_inventory.entity_id)
    assert moved_dry_contact is not None
    assert moved_dry_contact.device_id == gateway.id
    moved_entry = ent_reg.async_get(legacy_metric.entity_id)
    assert moved_entry is not None
    assert moved_entry.device_id == gateway.id
    moved_site_entry = ent_reg.async_get(site_metric.entity_id)
    assert moved_site_entry is not None
    assert moved_site_entry.device_id == gateway.id

    remove_device = getattr(dev_reg, "async_remove_device", None)
    if callable(remove_device):
        assert dev_reg.async_get(meter.id) is None
        assert dev_reg.async_get(enpower.id) is None
        assert dev_reg.async_get(dry_contact.id) is None
        assert dev_reg.async_get(site_device.id) is None


def test_sync_type_devices_skips_dry_contact_types(config_entry) -> None:
    dev_reg = SimpleNamespace(async_get_device=Mock(), async_get_or_create=Mock())
    coord = _with_inventory_view(
        SimpleNamespace(
            iter_type_keys=lambda: ["envoy", "dry_contact", "nc1"],
            type_identifier=lambda key: (DOMAIN, f"type:site-1:{key}"),
            type_label=lambda key: {
                "envoy": "Gateway",
                "dry_contact": "Dry Contacts",
            }.get(key, key),
            type_device_name=lambda key: {
                "envoy": "IQ Gateway",
                "dry_contact": "Dry Contacts",
            }.get(key, key),
        )
    )

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, "site-1")

    assert "envoy" in type_devices
    assert "dry_contact" not in type_devices
    assert "nc1" not in type_devices
    dev_reg.async_get_or_create.assert_called_once()


def test_sync_type_devices_skips_selected_type_without_bucket(config_entry) -> None:
    from custom_components.enphase_ev.inventory_view import InventoryView

    dev_reg = SimpleNamespace(async_get_device=Mock(), async_get_or_create=Mock())
    coord = SimpleNamespace(
        site_id="site-1",
        inventory_runtime=SimpleNamespace(),
        heatpump_runtime=SimpleNamespace(),
        _selected_type_keys={"iqevse"},
        _type_device_order=None,
        _type_device_buckets={},
    )
    coord.inventory_view = InventoryView(coord)

    type_devices = _sync_type_devices(config_entry, coord, dev_reg, "site-1")

    assert type_devices == {}
    dev_reg.async_get_or_create.assert_not_called()


def test_migrate_legacy_gateway_type_devices_handles_internal_edge_paths(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    module = importlib.import_module("custom_components.enphase_ev")

    # Cover guard path when entity registry helper is unavailable.
    original_er = module.er
    monkeypatch.setattr(module, "er", None)
    _migrate_legacy_gateway_type_devices(
        hass,
        config_entry,
        _with_inventory_view(
            SimpleNamespace(type_identifier=lambda _key: (DOMAIN, "type:x:envoy"))
        ),
        SimpleNamespace(async_get_device=lambda **_kwargs: None),
        "x",
    )
    monkeypatch.setattr(module, "er", original_er)

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    # Cover str(site_id) failure and blank-site early return.
    _migrate_legacy_gateway_type_devices(
        hass,
        config_entry,
        _with_inventory_view(
            SimpleNamespace(type_identifier=lambda _key: (DOMAIN, "type:x:envoy"))
        ),
        SimpleNamespace(async_get_device=lambda **_kwargs: None),
        BadStr(),
    )
    _migrate_legacy_gateway_type_devices(
        hass,
        config_entry,
        _with_inventory_view(
            SimpleNamespace(type_identifier=lambda _key: (DOMAIN, "type:x:envoy"))
        ),
        SimpleNamespace(async_get_device=lambda **_kwargs: None),
        "   ",
    )

    # Cover gateway-without-id early return.
    _migrate_legacy_gateway_type_devices(
        hass,
        config_entry,
        _with_inventory_view(
            SimpleNamespace(type_identifier=lambda _key: (DOMAIN, "type:x:envoy"))
        ),
        SimpleNamespace(async_get_device=lambda **_kwargs: SimpleNamespace(id=None)),
        "x",
    )

    # Cover entity registry acquisition failure.
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    _migrate_legacy_gateway_type_devices(
        hass,
        config_entry,
        _with_inventory_view(
            SimpleNamespace(type_identifier=lambda _key: (DOMAIN, "type:x:envoy"))
        ),
        SimpleNamespace(
            async_get_device=lambda **kwargs: (
                SimpleNamespace(id="gw")
                if next(iter(kwargs["identifiers"])) == (DOMAIN, "type:x:envoy")
                else None
            )
        ),
        "x",
    )

    # Cover site_id fallback, legacy device without id, missing entity_id branch,
    # and update-entity failure branch.
    entries = [
        SimpleNamespace(
            platform=DOMAIN, config_entry_id=config_entry.entry_id, entity_id=None
        ),
        SimpleNamespace(
            platform=DOMAIN,
            config_entry_id=config_entry.entry_id,
            entity_id="sensor.fail_move",
        ),
    ]
    ent_reg = SimpleNamespace(
        entities={f"e{idx}": entry for idx, entry in enumerate(entries)},
        async_remove=lambda _entity_id: None,
        async_update_entity=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("move failed")
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get", lambda _hass: ent_reg
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_entries_for_device",
        lambda _reg, _device_id: entries,
    )
    dev_reg = SimpleNamespace(
        async_get_device=lambda **kwargs: {
            (DOMAIN, "type:site-fallback:envoy"): SimpleNamespace(id="gw"),
            (DOMAIN, "type:site-fallback:meter"): SimpleNamespace(id="legacy-meter"),
            (DOMAIN, "type:site-fallback:enpower"): SimpleNamespace(id=None),
            (DOMAIN, "site:site-fallback"): SimpleNamespace(id=None),
        }.get(next(iter(kwargs["identifiers"]))),
        async_remove_device=lambda _device_id: None,
    )
    coord = _with_inventory_view(
        SimpleNamespace(
            site_id="site-fallback",
            type_identifier=lambda key: (DOMAIN, f"type:site-fallback:{key}"),
        )
    )

    _migrate_legacy_gateway_type_devices(hass, config_entry, coord, dev_reg, None)

    dev_reg_site_update = SimpleNamespace(
        async_get_device=lambda **kwargs: {
            (DOMAIN, "type:site-fallback:envoy"): SimpleNamespace(id="gw"),
            (DOMAIN, "site:site-fallback"): SimpleNamespace(id="legacy-site"),
        }.get(next(iter(kwargs["identifiers"]))),
        async_remove_device=lambda _device_id: None,
    )
    _migrate_legacy_gateway_type_devices(
        hass, config_entry, coord, dev_reg_site_update, None
    )

    dev_reg_scanned = SimpleNamespace(
        async_get_device=lambda **kwargs: {
            (DOMAIN, "type:site-fallback:envoy"): SimpleNamespace(id="gw"),
        }.get(next(iter(kwargs["identifiers"]))),
        async_remove_device=lambda _device_id: None,
        devices={
            "foreign": SimpleNamespace(
                id="foreign",
                config_entries={"other-entry"},
                identifiers={(DOMAIN, "type:site-fallback:nc1")},
            ),
            "missing-identifiers": SimpleNamespace(
                id="missing-identifiers",
                config_entries={config_entry.entry_id},
                identifiers=set(),
            ),
            "other-domain": SimpleNamespace(
                id="other-domain",
                config_entries={config_entry.entry_id},
                identifiers={("other", "type:site-fallback:nc1")},
            ),
        },
    )
    _migrate_legacy_gateway_type_devices(
        hass, config_entry, coord, dev_reg_scanned, None
    )


@pytest.mark.asyncio
async def test_migrate_cloud_entities_to_cloud_device_rehomes_known_entities(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    gateway = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="Gateway (1)",
    )
    cloud_last_update = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_last_update",
        device_id=gateway.id,
        config_entry=config_entry,
    )
    cloud_latency = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_latency_ms",
        device_id=gateway.id,
        config_entry=config_entry,
    )
    cloud_current_power = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_current_production_power",
        device_id=gateway.id,
        config_entry=config_entry,
    )
    cloud_error = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_last_error_code",
        device_id=gateway.id,
        config_entry=config_entry,
    )
    cloud_backoff = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_backoff_ends",
        device_id=gateway.id,
        config_entry=config_entry,
    )
    cloud_reachable = ent_reg.async_get_or_create(
        domain="binary_sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_cloud_reachable",
        device_id=gateway.id,
        config_entry=config_entry,
    )
    site_grid_import = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_grid_import",
        device_id=gateway.id,
        config_entry=config_entry,
    )
    site_grid_power = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_grid_power",
        device_id=gateway.id,
        config_entry=config_entry,
    )
    site_battery_power = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_battery_power",
        device_id=gateway.id,
        config_entry=config_entry,
    )

    disabler = getattr(er, "RegistryEntryDisabler", None)
    if disabler is not None:
        ent_reg.async_update_entity(cloud_backoff.entity_id, disabled_by=disabler.USER)
        ent_reg.async_update_entity(
            site_grid_import.entity_id, disabled_by=disabler.INTEGRATION
        )
        ent_reg.async_update_entity(
            site_grid_power.entity_id, disabled_by=disabler.INTEGRATION
        )
        ent_reg.async_update_entity(
            site_battery_power.entity_id, disabled_by=disabler.INTEGRATION
        )

    coord = SimpleNamespace(site_id=site_id)
    _migrate_cloud_entities_to_cloud_device(hass, config_entry, coord, dev_reg, None)

    cloud_device = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"type:{site_id}:cloud")}
    )
    assert cloud_device is not None

    for entity_id in (
        cloud_last_update.entity_id,
        cloud_latency.entity_id,
        cloud_current_power.entity_id,
        cloud_error.entity_id,
        cloud_backoff.entity_id,
        cloud_reachable.entity_id,
        site_grid_import.entity_id,
        site_grid_power.entity_id,
        site_battery_power.entity_id,
    ):
        reg_entry = ent_reg.async_get(entity_id)
        assert reg_entry is not None
        assert reg_entry.device_id == cloud_device.id

    if disabler is not None:
        reg_entry = ent_reg.async_get(cloud_backoff.entity_id)
        assert reg_entry is not None
        assert reg_entry.disabled_by is disabler.USER
        site_reg_entry = ent_reg.async_get(site_grid_import.entity_id)
        assert site_reg_entry is not None
        assert site_reg_entry.disabled_by is None
        site_grid_power_reg_entry = ent_reg.async_get(site_grid_power.entity_id)
        assert site_grid_power_reg_entry is not None
        assert site_grid_power_reg_entry.disabled_by is None
        site_battery_power_reg_entry = ent_reg.async_get(site_battery_power.entity_id)
        assert site_battery_power_reg_entry is not None
        assert site_battery_power_reg_entry.disabled_by is None


@pytest.mark.asyncio
async def test_migrate_cloud_entity_unique_ids_preserves_legacy_entity_id(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    ent_reg = er.async_get(hass)
    legacy = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_current_power_consumption",
        config_entry=config_entry,
        original_name="Current Power Consumption",
    )

    _migrate_cloud_entity_unique_ids(hass, config_entry, site_id)

    migrated = ent_reg.async_get(legacy.entity_id)
    assert migrated is not None
    assert migrated.entity_id == legacy.entity_id
    assert migrated.unique_id == f"{DOMAIN}_site_{site_id}_current_production_power"


@pytest.mark.asyncio
async def test_migrate_cloud_entity_unique_ids_updates_cloud_last_error_alias(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    ent_reg = er.async_get(hass)
    legacy = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{site_id}_cloud_last_error",
        config_entry=config_entry,
        original_name="Cloud Last Error",
    )

    _migrate_cloud_entity_unique_ids(hass, config_entry, site_id)

    migrated = ent_reg.async_get(legacy.entity_id)
    assert migrated is not None
    assert migrated.entity_id == legacy.entity_id
    assert migrated.unique_id == f"{DOMAIN}_site_{site_id}_last_error_code"


@pytest.mark.asyncio
async def test_migrate_cloud_entity_unique_ids_prefers_error_code_alias_and_prunes_duplicates(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    ent_reg = er.async_get(hass)
    preferred = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{site_id}_cloud_last_error_code",
        config_entry=config_entry,
        original_name="Cloud Last Error Code",
    )
    duplicate_alias = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{site_id}_cloud_last_error",
        config_entry=config_entry,
        original_name="Cloud Last Error",
    )
    duplicate_target = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_last_error_code",
        config_entry=config_entry,
        original_name="Cloud Error Code",
    )

    _migrate_cloud_entity_unique_ids(hass, config_entry, site_id)

    migrated = ent_reg.async_get(preferred.entity_id)
    assert migrated is not None
    assert migrated.entity_id == preferred.entity_id
    assert migrated.unique_id == f"{DOMAIN}_site_{site_id}_last_error_code"
    assert ent_reg.async_get(duplicate_alias.entity_id) is None
    assert ent_reg.async_get(duplicate_target.entity_id) is None


def test_migrate_cloud_entity_unique_ids_handles_guard_paths(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]

    monkeypatch.setattr(enphase_init, "er", None)
    _migrate_cloud_entity_unique_ids(hass, config_entry, site_id)

    class BadSiteId:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    monkeypatch.setattr(enphase_init, "er", er)
    _migrate_cloud_entity_unique_ids(hass, config_entry, BadSiteId())
    _migrate_cloud_entity_unique_ids(hass, config_entry, "   ")

    class RaisingRegistryModule:
        @staticmethod
        def async_get(_hass):
            raise RuntimeError("boom")

    monkeypatch.setattr(enphase_init, "er", RaisingRegistryModule())
    _migrate_cloud_entity_unique_ids(hass, config_entry, site_id)


@pytest.mark.asyncio
async def test_migrate_cloud_entity_unique_ids_removes_duplicate_new_entity(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    ent_reg = er.async_get(hass)
    legacy = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_current_power_consumption",
        config_entry=config_entry,
        original_name="Current Power Consumption",
    )
    duplicate = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_current_production_power",
        config_entry=config_entry,
        original_name="Current Production Power",
    )

    _migrate_cloud_entity_unique_ids(hass, config_entry, site_id)

    assert ent_reg.async_get(duplicate.entity_id) is None
    migrated = ent_reg.async_get(legacy.entity_id)
    assert migrated is not None
    assert migrated.unique_id == f"{DOMAIN}_site_{site_id}_current_production_power"


def test_migrate_cloud_entity_unique_ids_handles_duplicate_remove_failure(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_current_power_consumption",
        config_entry=config_entry,
        original_name="Current Power Consumption",
    )
    duplicate = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_current_production_power",
        config_entry=config_entry,
        original_name="Current Production Power",
    )

    original_remove = ent_reg.async_remove

    def _raise_remove(entity_id: str) -> None:
        if entity_id == duplicate.entity_id:
            raise RuntimeError("boom")
        original_remove(entity_id)

    monkeypatch.setattr(ent_reg, "async_remove", _raise_remove)
    _migrate_cloud_entity_unique_ids(hass, config_entry, site_id)


def test_migrate_cloud_entity_unique_ids_handles_duplicate_alias_remove_failure(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    ent_reg = er.async_get(hass)
    preferred = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{site_id}_cloud_last_error_code",
        config_entry=config_entry,
        original_name="Cloud Last Error Code",
    )
    duplicate_alias = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{site_id}_cloud_last_error",
        config_entry=config_entry,
        original_name="Cloud Last Error",
    )

    original_remove = ent_reg.async_remove

    def _raise_remove(entity_id: str) -> None:
        if entity_id == duplicate_alias.entity_id:
            raise RuntimeError("boom")
        original_remove(entity_id)

    monkeypatch.setattr(ent_reg, "async_remove", _raise_remove)
    _migrate_cloud_entity_unique_ids(hass, config_entry, site_id)

    reg_entry = ent_reg.async_get(preferred.entity_id)
    assert reg_entry is not None
    assert reg_entry.unique_id == f"{DOMAIN}_site_{site_id}_last_error_code"
    assert ent_reg.async_get(duplicate_alias.entity_id) is not None


def test_migrate_cloud_entity_unique_ids_handles_update_failure(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    ent_reg = er.async_get(hass)
    legacy = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_current_power_consumption",
        config_entry=config_entry,
        original_name="Current Power Consumption",
    )

    def _raise_update(*args, **kwargs) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(ent_reg, "async_update_entity", _raise_update)
    _migrate_cloud_entity_unique_ids(hass, config_entry, site_id)

    reg_entry = ent_reg.async_get(legacy.entity_id)
    assert reg_entry is not None
    assert reg_entry.unique_id == f"{DOMAIN}_site_{site_id}_current_power_consumption"


@pytest.mark.asyncio
async def test_migrate_cloud_entities_to_cloud_device_rehomes_legacy_cloud_suffix(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    gateway = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="Gateway (1)",
    )
    legacy_cloud_error = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_{site_id}_cloud_last_error",
        device_id=gateway.id,
        config_entry=config_entry,
    )

    coord = SimpleNamespace(site_id=site_id)
    _migrate_cloud_entities_to_cloud_device(hass, config_entry, coord, dev_reg, None)

    cloud_device = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"type:{site_id}:cloud")}
    )
    assert cloud_device is not None
    reg_entry = ent_reg.async_get(legacy_cloud_error.entity_id)
    assert reg_entry is not None
    assert reg_entry.device_id == cloud_device.id


def test_migrate_cloud_entities_to_cloud_device_handles_edge_paths(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    module = importlib.import_module("custom_components.enphase_ev")
    original_er = module.er

    monkeypatch.setattr(module, "er", None)
    _migrate_cloud_entities_to_cloud_device(
        hass, config_entry, SimpleNamespace(site_id="site"), object(), "site"
    )
    monkeypatch.setattr(module, "er", original_er)

    class BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    _migrate_cloud_entities_to_cloud_device(
        hass, config_entry, SimpleNamespace(site_id="site"), object(), BadStr()
    )
    _migrate_cloud_entities_to_cloud_device(
        hass, config_entry, SimpleNamespace(site_id="   "), object(), "   "
    )

    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    _migrate_cloud_entities_to_cloud_device(
        hass,
        config_entry,
        SimpleNamespace(site_id="site-1"),
        SimpleNamespace(async_get_or_create=lambda **_kwargs: None),
        "site-1",
    )

    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: SimpleNamespace(),
    )
    _migrate_cloud_entities_to_cloud_device(
        hass,
        config_entry,
        SimpleNamespace(site_id="site-1"),
        SimpleNamespace(),
        "site-1",
    )

    ent_reg = SimpleNamespace(
        async_get_entity_id=lambda _domain, _platform, _unique_id: "sensor.fail",
        async_get=lambda _entity_id: SimpleNamespace(
            device_id="legacy",
            platform=DOMAIN,
            config_entry_id=config_entry.entry_id,
        ),
        async_update_entity=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("move failed")
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: ent_reg,
    )
    _migrate_cloud_entities_to_cloud_device(
        hass,
        config_entry,
        SimpleNamespace(site_id="site-2"),
        SimpleNamespace(async_get_or_create=lambda **_kwargs: SimpleNamespace(id=None)),
        "site-2",
    )
    _migrate_cloud_entities_to_cloud_device(
        hass,
        config_entry,
        SimpleNamespace(site_id="site-3"),
        SimpleNamespace(
            async_get_or_create=lambda **_kwargs: SimpleNamespace(id="cloud-device")
        ),
        "site-3",
    )

    ent_reg_same_device = SimpleNamespace(
        async_get_entity_id=lambda _domain, _platform, unique_id: (
            "binary_sensor.cloud" if unique_id.endswith("_cloud_reachable") else None
        ),
        async_get=lambda _entity_id: SimpleNamespace(
            device_id="cloud-device",
            platform=DOMAIN,
            config_entry_id=config_entry.entry_id,
        ),
        async_update_entity=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("should not update")
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: ent_reg_same_device,
    )
    _migrate_cloud_entities_to_cloud_device(
        hass,
        config_entry,
        SimpleNamespace(site_id="site-4"),
        SimpleNamespace(
            async_get_or_create=lambda **_kwargs: SimpleNamespace(id="cloud-device")
        ),
        "site-4",
    )


def test_migrate_cloud_entities_to_cloud_device_cloud_info_fallbacks(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    captured: dict[str, object] = {}

    def _create_device(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="cloud-device")

    ent_reg = SimpleNamespace(
        async_get_entity_id=lambda *_args, **_kwargs: None,
        async_get=lambda *_args, **_kwargs: None,
        async_update_entity=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get", lambda _hass: ent_reg
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev._cloud_device_info",
        lambda _site_id: {"model": object(), "sw_version": object()},
    )

    _migrate_cloud_entities_to_cloud_device(
        hass,
        config_entry,
        SimpleNamespace(site_id=site_id),
        SimpleNamespace(async_get_or_create=_create_device),
        site_id,
    )

    assert captured["model"] == "Cloud Service"
    assert captured["sw_version"] is None


def test_is_disabled_by_integration_handles_bad_string_value() -> None:
    class BadValue:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    assert _is_disabled_by_integration(BadValue()) is False


def test_is_disabled_by_integration_handles_none() -> None:
    assert _is_disabled_by_integration(None) is False


def test_migrate_cloud_entities_to_cloud_device_fallback_sweep_branch_coverage(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    updates: list[tuple[str, dict[str, object]]] = []

    fake_entries = {
        "a": SimpleNamespace(
            platform="other",
            config_entry_id=config_entry.entry_id,
            entity_id="sensor.not_owned",
            domain="sensor",
            unique_id=f"{DOMAIN}_site_{site_id}_last_error_code",
        ),
        "b": SimpleNamespace(
            platform=DOMAIN,
            config_entry_id=config_entry.entry_id,
            entity_id=None,
            domain="sensor",
            unique_id=f"{DOMAIN}_site_{site_id}_last_error_code",
        ),
        "c": SimpleNamespace(
            platform=DOMAIN,
            config_entry_id=config_entry.entry_id,
            entity_id="sensor.domain_from_entity_id",
            domain=None,
            unique_id="",
        ),
        "d": SimpleNamespace(
            platform=DOMAIN,
            config_entry_id=config_entry.entry_id,
            entity_id="switch.not_cloud_domain",
            domain="switch",
            unique_id=f"{DOMAIN}_site_{site_id}_last_error_code",
        ),
        "e": SimpleNamespace(
            platform=DOMAIN,
            config_entry_id=config_entry.entry_id,
            entity_id="sensor.no_unique_id",
            domain="sensor",
            unique_id=None,
        ),
        "f": SimpleNamespace(
            platform=DOMAIN,
            config_entry_id=config_entry.entry_id,
            entity_id="sensor.other_site",
            domain="sensor",
            unique_id=f"{DOMAIN}_site_other_last_error_code",
        ),
        "g": SimpleNamespace(
            platform=DOMAIN,
            config_entry_id=config_entry.entry_id,
            entity_id="sensor.unmatched_suffix",
            domain="sensor",
            unique_id=f"{DOMAIN}_site_{site_id}_unmatched_suffix",
        ),
    }
    fake_registry = SimpleNamespace(
        entities=fake_entries,
        async_get_entity_id=lambda *_args, **_kwargs: None,
        async_get=lambda entity_id: next(
            (entry for entry in fake_entries.values() if entry.entity_id == entity_id),
            None,
        ),
        async_update_entity=lambda entity_id, **kwargs: updates.append(
            (entity_id, dict(kwargs))
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get",
        lambda _hass: fake_registry,
    )

    _migrate_cloud_entities_to_cloud_device(
        hass,
        config_entry,
        SimpleNamespace(site_id=site_id),
        SimpleNamespace(
            async_get_or_create=lambda **_kwargs: SimpleNamespace(id="cloud-device")
        ),
        None,
    )

    assert updates == []


def test_migrate_cloud_entities_to_cloud_device_skips_updates_when_no_changes_needed(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    ent_reg = SimpleNamespace(
        async_get_entity_id=lambda domain, _platform, unique_id: (
            "sensor.cloud_last_update"
            if domain == "sensor" and unique_id.endswith("_last_update")
            else None
        ),
        async_get=lambda entity_id: SimpleNamespace(
            entity_id=entity_id,
            device_id="cloud-device",
            disabled_by=None,
            platform=DOMAIN,
            config_entry_id=config_entry.entry_id,
        ),
        async_update_entity=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("should not update")
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get", lambda _hass: ent_reg
    )

    _migrate_cloud_entities_to_cloud_device(
        hass,
        config_entry,
        SimpleNamespace(site_id="site-noop"),
        SimpleNamespace(
            async_get_or_create=lambda **_kwargs: SimpleNamespace(id="cloud-device")
        ),
        "site-noop",
    )


def test_migrate_cloud_entities_to_cloud_device_logs_update_failures(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    ent_reg = SimpleNamespace(
        async_get_entity_id=lambda domain, _platform, unique_id: (
            "sensor.cloud_last_update"
            if domain == "sensor" and unique_id.endswith("_last_update")
            else None
        ),
        async_get=lambda entity_id: SimpleNamespace(
            entity_id=entity_id,
            device_id="legacy-device",
            disabled_by=None,
            platform=DOMAIN,
            config_entry_id=config_entry.entry_id,
        ),
        async_update_entity=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.er.async_get", lambda _hass: ent_reg
    )

    _migrate_cloud_entities_to_cloud_device(
        hass,
        config_entry,
        SimpleNamespace(site_id="site-fail"),
        SimpleNamespace(
            async_get_or_create=lambda **_kwargs: SimpleNamespace(id="cloud-device")
        ),
        "site-fail",
    )


@pytest.mark.asyncio
async def test_migrate_legacy_gateway_type_devices_skips_without_gateway(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    meter = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:meter")},
        manufacturer="Enphase",
        name="Meter (1)",
    )
    legacy = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_legacy_metric_no_gateway",
        device_id=meter.id,
        config_entry=config_entry,
    )
    coord = _with_inventory_view(
        SimpleNamespace(
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
        )
    )

    _migrate_legacy_gateway_type_devices(hass, config_entry, coord, dev_reg, site_id)

    assert ent_reg.async_get(legacy.entity_id) is not None
    assert ent_reg.async_get(legacy.entity_id).device_id == meter.id


@pytest.mark.asyncio
async def test_migrate_legacy_gateway_type_devices_keeps_unowned_entities(
    hass: HomeAssistant, config_entry
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    gateway = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="Gateway (1)",
    )
    meter = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:meter")},
        manufacturer="Enphase",
        name="Meter (1)",
    )
    site_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="Enphase",
        name=f"Enphase Site {site_id}",
    )

    owned = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_owned_metric",
        device_id=meter.id,
        config_entry=config_entry,
    )
    other_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SITE_ID: "other-site"},
        title="Other",
        unique_id="other-entry",
    )
    other_entry.add_to_hass(hass)
    foreign = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_foreign_metric",
        device_id=meter.id,
        config_entry=other_entry,
    )
    foreign_inventory = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_type_meter_inventory",
        device_id=meter.id,
        config_entry=other_entry,
    )
    foreign_site_entity = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_foreign_site_metric",
        device_id=site_device.id,
        config_entry=other_entry,
    )

    coord = _with_inventory_view(
        SimpleNamespace(
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
        )
    )
    _migrate_legacy_gateway_type_devices(hass, config_entry, coord, dev_reg, site_id)

    owned_entry = ent_reg.async_get(owned.entity_id)
    assert owned_entry is not None
    assert owned_entry.device_id == gateway.id
    foreign_entry = ent_reg.async_get(foreign.entity_id)
    assert foreign_entry is not None
    assert foreign_entry.device_id == meter.id
    foreign_inventory_entry = ent_reg.async_get(foreign_inventory.entity_id)
    assert foreign_inventory_entry is not None
    assert foreign_inventory_entry.device_id == meter.id
    foreign_site_entry = ent_reg.async_get(foreign_site_entity.entity_id)
    assert foreign_site_entry is not None
    assert foreign_site_entry.device_id == site_device.id
    assert dev_reg.async_get(meter.id) is not None
    assert dev_reg.async_get(site_device.id) is not None


@pytest.mark.asyncio
async def test_migrate_legacy_gateway_type_devices_handles_remove_failure(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    gateway = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:envoy")},
        manufacturer="Enphase",
        name="Gateway (1)",
    )
    enpower = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{site_id}:enpower")},
        manufacturer="Enphase",
        name="System Controller (1)",
    )
    site_device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{site_id}")},
        manufacturer="Enphase",
        name=f"Enphase Site {site_id}",
    )
    moved = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_remove_failure_metric",
        device_id=enpower.id,
        config_entry=config_entry,
    )
    moved_site = ent_reg.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=f"{DOMAIN}_site_{site_id}_remove_failure_site_metric",
        device_id=site_device.id,
        config_entry=config_entry,
    )

    def _boom(_device_id: str) -> None:
        raise RuntimeError("cannot remove")

    monkeypatch.setattr(dev_reg, "async_remove_device", _boom)
    coord = _with_inventory_view(
        SimpleNamespace(
            type_identifier=lambda key: (DOMAIN, f"type:{site_id}:{key}"),
        )
    )

    _migrate_legacy_gateway_type_devices(hass, config_entry, coord, dev_reg, site_id)

    moved_entry = ent_reg.async_get(moved.entity_id)
    assert moved_entry is not None
    assert moved_entry.device_id == gateway.id
    moved_site_entry = ent_reg.async_get(moved_site.entity_id)
    assert moved_site_entry is not None
    assert moved_site_entry.device_id == gateway.id


@pytest.mark.asyncio
async def test_async_setup_entry_registry_sync_runs_only_for_topology_updates(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    topology_listeners: list = []
    state_listeners: list = []

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.serials = {RANDOM_SERIAL}
            self.data = {RANDOM_SERIAL: {"name": "Fallback Charger"}}
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())
            self._startup_ready = False

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return [RANDOM_SERIAL]

        def iter_type_keys(self) -> list[str]:
            return ["iqevse"]

        def type_identifier(self, type_key: str):
            return type_identifier(self.site_id, type_key)

        def type_label(self, _type_key: str) -> str:
            return "EV Chargers"

        def type_device_name(self, _type_key: str) -> str:
            return "EV Chargers (1)"

        def startup_migrations_ready(self) -> bool:
            return self._startup_ready

        def async_add_topology_listener(self, callback):
            topology_listeners.append(callback)
            return lambda: None

        def async_add_listener(self, callback):
            state_listeners.append(callback)
            return lambda: None

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    forward = AsyncMock()
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", forward)
    sync_registry_devices = Mock()
    monkeypatch.setattr(
        "custom_components.enphase_ev._sync_registry_devices", sync_registry_devices
    )
    migrate = Mock()
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_legacy_gateway_type_devices", migrate
    )
    migrate_updates = Mock()
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_orphaned_update_entities_to_type_devices",
        migrate_updates,
    )
    migrate_cloud = Mock()
    monkeypatch.setattr(
        "custom_components.enphase_ev._migrate_cloud_entities_to_cloud_device",
        migrate_cloud,
    )

    assert await async_setup_entry(hass, config_entry)
    assert topology_listeners, "expected setup to register a topology listener"
    assert state_listeners, "expected setup to register a state listener"
    assert migrate.call_count == 0
    assert migrate_updates.call_count == 0
    assert migrate_cloud.call_count == 0
    assert "startup_migration_version" not in config_entry.data
    assert sync_registry_devices.call_count == 1

    dummy_coord._startup_ready = True
    topology_listeners[0]()

    assert sync_registry_devices.call_count == 1
    assert migrate.call_count == 1
    assert migrate_updates.call_count == 1
    assert migrate_cloud.call_count == 1
    assert config_entry.data["startup_migration_version"] == 6

    for listener in state_listeners:
        listener()
    assert sync_registry_devices.call_count == 1

    dummy_coord.data[RANDOM_SERIAL]["sw_version"] = "1.2.3"
    topology_listeners[0]()
    assert sync_registry_devices.call_count == 2
    assert migrate_updates.call_count == 1


@pytest.mark.asyncio
async def test_async_setup_entry_registry_sync_listener_swallows_sync_errors(
    hass: HomeAssistant, config_entry, monkeypatch
) -> None:
    site_id = config_entry.data[CONF_SITE_ID]
    topology_listeners: list = []
    state_listeners: list = []

    class DummyCoordinator:
        def __init__(self) -> None:
            self.site_id = site_id
            self.serials = {RANDOM_SERIAL}
            self.data = {RANDOM_SERIAL: {"name": "Fallback Charger"}}
            self.schedule_sync = SimpleNamespace(async_start=AsyncMock())

        async def async_config_entry_first_refresh(self) -> None:
            return None

        def iter_serials(self) -> list[str]:
            return [RANDOM_SERIAL]

        def iter_type_keys(self) -> list[str]:
            return ["iqevse"]

        def type_identifier(self, type_key: str):
            return type_identifier(self.site_id, type_key)

        def type_label(self, _type_key: str) -> str:
            return "EV Chargers"

        def type_device_name(self, _type_key: str) -> str:
            return "EV Chargers (1)"

        def async_add_topology_listener(self, callback):
            topology_listeners.append(callback)
            return lambda: None

        def async_add_listener(self, callback):
            state_listeners.append(callback)
            return lambda: None

    dummy_coord = _with_inventory_view(DummyCoordinator())
    monkeypatch.setattr(
        "custom_components.enphase_ev.coordinator.EnphaseCoordinator",
        lambda hass_, entry_data, config_entry=None, **kwargs: dummy_coord,
    )
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr(
        "custom_components.enphase_ev._registry_metadata_signature",
        Mock(side_effect=["initial-signature", RuntimeError("boom")]),
    )

    assert await async_setup_entry(hass, config_entry)
    assert state_listeners
    assert topology_listeners

    topology_listeners[0]()
