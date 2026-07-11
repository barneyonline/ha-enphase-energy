from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.components.update import UpdateEntityDescription
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from unittest.mock import AsyncMock, MagicMock

from custom_components.enphase_ev import PLATFORMS
from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from custom_components.enphase_ev.update import (
    ChargerFirmwareUpdateEntity,
    FirmwareVersionHistoryStore,
    FirmwareUpdateEntity,
    _async_prune_removed_charger_updates,
    _as_bool,
    _as_int,
    _charger_serials,
    _charger_installed_version,
    _charger_update_unique_id,
    _evse_firmware_rollout_enabled,
    _gateway_installed_version,
    _text,
    _type_available,
    async_setup_entry,
)

TEST_EVSE_SERIAL = "EVSE-SERIAL-0001"
TEST_EVSE_SITE_ID = 1234567


class DummyCatalogManager:
    def __init__(self, catalog):
        self.cached_catalog = catalog
        self._status = {
            "last_fetch_utc": "2026-03-01T00:00:00+00:00",
            "last_error": None,
            "catalog_generated_at": catalog.get("generated_at"),
            "catalog_source_age_seconds": 30.0,
            "using_stale": False,
        }

    async def async_get_catalog(self, *, force_refresh: bool = False):  # noqa: ARG002
        return self.cached_catalog

    def status_snapshot(self):
        return dict(self._status)


class DummyEvseFirmwareManager:
    def __init__(self, details):
        self.cached_details = details
        self._status = {
            "cache_expires_utc": "2026-03-01T01:00:00+00:00",
            "last_fetch_utc": "2026-03-01T00:00:00+00:00",
            "last_success_utc": "2026-03-01T00:00:00+00:00",
            "last_error": None,
            "using_stale": False,
        }

    async def async_get_details(self, *, force_refresh: bool = False):  # noqa: ARG002
        return self.cached_details

    def status_snapshot(self):
        return dict(self._status)


class DummyGatewayUpdateManager:
    def __init__(self, status=None):
        self.cached_status = status
        self.next_refresh_seconds = 60
        self._status = {
            "last_fetch_utc": "2026-07-11T05:06:08+00:00",
            "last_success_utc": "2026-07-11T05:06:08+00:00",
            "last_error": None,
            "using_stale": False,
        }
        self.async_get_status = AsyncMock(return_value=status)

    def status_snapshot(self):
        return dict(self._status)


class DummyCoordinator:
    def __init__(self) -> None:
        self.site_id = "12345"
        self.last_update_success = True
        self.battery_country_code = "AU"
        self.battery_locale = "fr-fr"
        self._gateway_version = "8.2.4300"
        self._iqevse_version = "25.37.1.13"
        self._listeners = []
        self._available_types = {"envoy", "microinverter", "iqevse"}
        self._devices_inventory_ready = False
        self._active_evse_serials: list[str] | None = None
        self._evse_feature_flags = {
            TEST_EVSE_SERIAL: {"iqevse_itk_fw_upgrade_status": True}
        }
        self.data = {
            TEST_EVSE_SERIAL: {
                "firmware_version": "25.37.1.13",
                "system_version": "25.37.1.13",
                "display_name": "Driveway Charger",
            }
        }
        self.inventory_view = SimpleNamespace(
            has_type_for_entities=self.has_type_for_entities,
            has_type=self.has_type,
            type_bucket=self.type_bucket,
            type_device_info=self.type_device_info,
            type_device_sw_version=self.type_device_sw_version,
        )

    def async_add_listener(self, callback):
        self._listeners.append(callback)

        def _remove_listener():
            self._listeners.remove(callback)

        return _remove_listener

    def has_type_for_entities(self, type_key: str) -> bool:
        return type_key in self._available_types

    def has_type(self, type_key: str) -> bool:
        return self.has_type_for_entities(type_key)

    def type_device_sw_version(self, type_key: str) -> str | None:
        if type_key == "envoy":
            return self._gateway_version
        if type_key == "iqevse":
            return self._iqevse_version
        return None

    def type_bucket(self, type_key: str):
        return None

    def type_device_info(self, type_key: str):
        return {
            "identifiers": {("enphase_ev", f"type:{self.site_id}:{type_key}")},
            "name": f"{type_key} device",
            "manufacturer": "Enphase",
            "model": "Model",
        }

    def iter_serials(self) -> list[str]:
        return list(self.data)

    def _active_inventory_evse_serials(self) -> list[str] | None:
        if self._active_evse_serials is not None:
            return self._active_evse_serials
        return list(self.data)

    def evse_feature_flag_enabled(self, key: str, sn: str | None = None) -> bool | None:
        if sn is None:
            return None
        return self._evse_feature_flags.get(sn, {}).get(key)


def _catalog_payload() -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "devices": {
            "envoy": {
                "latest_by_locale": {
                    "fr-fr": {
                        "version": "8.2.4401",
                        "summary": "Gateway firmware update",
                        "urls_by_locale": {"fr-fr": "https://example.test/envoy/fr"},
                    }
                },
                "latest_by_country": {
                    "AU": {
                        "version": "8.2.4401",
                        "summary": "Gateway firmware update",
                        "urls_by_locale": {
                            "en": "https://example.test/envoy/en",
                            "fr-fr": "https://example.test/envoy/fr",
                        },
                    }
                },
                "latest_global": {
                    "version": "8.2.4400",
                    "summary": "Gateway global",
                    "urls_by_locale": {"en": "https://example.test/envoy/global"},
                },
            },
            "iqevse": {
                "latest_by_locale": {
                    "fr-fr": {
                        "version": "25.37.1.14",
                        "summary": "Charger firmware update",
                        "urls_by_locale": {"fr-fr": "https://example.test/charger/fr"},
                    }
                },
                "latest_by_country": {
                    "AU": {
                        "version": "25.37.1.14",
                        "summary": "Charger firmware update",
                        "urls_by_locale": {
                            "en": "https://example.test/charger/en",
                            "fr-fr": "https://example.test/charger/fr",
                        },
                    }
                },
                "latest_global": {
                    "version": "25.37.1.14",
                    "summary": "Charger global",
                    "urls_by_locale": {"en": "https://example.test/charger/global"},
                },
            },
        },
    }


def _evse_payload() -> dict[str, dict]:
    return {
        TEST_EVSE_SERIAL: {
            "serialNumber": TEST_EVSE_SERIAL,
            "siteId": TEST_EVSE_SITE_ID,
            "upgradeStatus": 5,
            "currentFwVersion": "25.37.1.13",
            "targetFwVersion": "25.37.1.14",
            "lastSuccessfulUpgradeDate": "2025-12-08T22:41:46.568837098Z[UTC]",
            "lastUpdatedAt": "2025-12-08T15:52:59.806385175Z[UTC]",
            "statusDetail": None,
            "isAutoOta": False,
        }
    }


def test_platform_enables_update_entities() -> None:
    assert "update" in PLATFORMS


@pytest.mark.asyncio
async def test_async_setup_entry_adds_firmware_update_entities(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    catalog_manager = DummyCatalogManager(_catalog_payload())
    evse_manager = DummyEvseFirmwareManager(_evse_payload())
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=catalog_manager,
        evse_firmware_details=evse_manager,
        gateway_software_update=DummyGatewayUpdateManager(),
    )

    added = []

    def _capture(entities, update_before_add=False):  # noqa: ARG001
        added.extend(entities)

    await async_setup_entry(hass, config_entry, _capture)

    assert len(added) == 2
    unique_ids = {entity.unique_id for entity in added}
    assert f"enphase_ev_site_{coord.site_id}_envoy_firmware" in unique_ids
    assert f"enphase_ev_{TEST_EVSE_SERIAL}_charger_firmware" in unique_ids
    gateway = next(
        entity for entity in added if isinstance(entity, FirmwareUpdateEntity)
    )
    assert (
        gateway._progress_manager is config_entry.runtime_data.gateway_software_update
    )


@pytest.mark.asyncio
async def test_gateway_update_entity_exposes_live_progress_and_sanitized_details(
    hass, monkeypatch
) -> None:
    progress_status = {
        "current_status": 2,
        "current_status_text": "Installing",
        "last_status": 0,
        "last_status_text": "Completed",
        "estimated_time_left": "1 min",
        "estimated_time_left_seconds": 60,
        "total_duration": "5 min",
        "total_duration_seconds": 300,
        "installed_image_version": "8.3.6000",
        "last_reported_at": "2026-07-11T05:06:07Z",
        "device_statuses": ["Gateway updating"],
        "component_updates": [
            {
                "name": "Gateway OS",
                "type": "essimg",
                "status": 2,
                "status_text": "Installing",
                "progress": 55.0,
                "latest_speed_bps": 1024,
            }
        ],
        "e3_progress": 55.0,
        "transfer_speed_bps": 1024,
        "in_progress": True,
        "update_percentage": 55.0,
    }
    progress_manager = DummyGatewayUpdateManager(progress_status)
    entity = FirmwareUpdateEntity(
        coordinator=DummyCoordinator(),
        manager=DummyCatalogManager(_catalog_payload()),
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
        progress_manager=progress_manager,
    )
    entity.hass = hass
    entity._apply_progress(progress_status)

    assert entity.in_progress is True
    assert entity.update_percentage == 55
    attrs = entity.extra_state_attributes
    assert attrs["software_update_current_status"] == "Installing"
    assert attrs["software_update_current_status_code"] == 2
    assert attrs["software_update_last_status"] == "Completed"
    assert attrs["software_update_last_status_code"] == 0
    assert attrs["estimated_time_left"] == "1 min"
    assert attrs["estimated_time_left_seconds"] == 60
    assert attrs["total_update_duration"] == "5 min"
    assert attrs["total_update_duration_seconds"] == 300
    assert attrs["installed_image_version"] == "8.3.6000"
    assert attrs["software_update_last_reported_at"] == "2026-07-11T05:06:07Z"
    assert attrs["software_update_device_statuses"] == ["Gateway updating"]
    assert attrs["software_update_components"][0]["name"] == "Gateway OS"
    assert attrs["software_update_e3_progress"] == 55
    assert attrs["software_update_transfer_speed_bps"] == 1024
    assert attrs["software_update_last_error"] is None
    assert attrs["software_update_using_stale"] is False
    assert "fw_image" not in repr(attrs)

    refresh_started = asyncio.Event()

    async def _get_status():
        refresh_started.set()
        return progress_status

    progress_manager.async_get_status = AsyncMock(side_effect=_get_status)
    write_state = MagicMock()
    monkeypatch.setattr(entity, "async_write_ha_state", write_state)
    refresh_task = hass.async_create_task(entity._async_refresh_progress_loop())
    await refresh_started.wait()
    await asyncio.sleep(0)
    refresh_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task
    progress_manager.async_get_status.assert_awaited_once_with()
    write_state.assert_called_once_with()

    entity._apply_progress(None)
    assert entity.in_progress is None
    assert entity.update_percentage is None
    assert "software_update_components" not in entity.extra_state_attributes


@pytest.mark.asyncio
async def test_gateway_update_entity_progress_task_lifecycle(hass, monkeypatch) -> None:
    progress_manager = DummyGatewayUpdateManager(
        {"in_progress": False, "update_percentage": None}
    )
    entity = FirmwareUpdateEntity(
        coordinator=DummyCoordinator(),
        manager=DummyCatalogManager(_catalog_payload()),
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
        progress_manager=progress_manager,
    )
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    monkeypatch.setattr(
        CoordinatorEntity, "_handle_coordinator_update", lambda self: None
    )
    remove = AsyncMock(return_value=None)
    monkeypatch.setattr(CoordinatorEntity, "async_will_remove_from_hass", remove)

    entity._schedule_progress_refresh()
    task = entity._progress_refresh_task
    assert task is not None
    await asyncio.sleep(0)
    entity._schedule_progress_refresh()
    assert entity._progress_refresh_task is task

    entity._handle_coordinator_update()
    assert entity.in_progress is False

    await entity.async_will_remove_from_hass()
    assert task.done()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert entity._progress_refresh_task is None
    remove.assert_awaited_once_with()

    entity.hass = None
    entity._schedule_progress_refresh()
    assert entity._progress_refresh_task is None


@pytest.mark.asyncio
async def test_async_setup_entry_skips_when_types_unavailable(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord._available_types = set()
    catalog_manager = DummyCatalogManager(_catalog_payload())
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=catalog_manager,
        evse_firmware_details=DummyEvseFirmwareManager(_evse_payload()),
    )
    added = []
    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(
            entities
        ),  # noqa: ARG005
    )
    assert added == []


@pytest.mark.asyncio
async def test_async_setup_entry_skips_charger_entities_when_no_serials(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord._available_types = {"iqevse"}
    coord.data = {}
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=DummyCatalogManager(_catalog_payload()),
        evse_firmware_details=DummyEvseFirmwareManager(_evse_payload()),
    )

    added = []
    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: added.extend(
            entities
        ),  # noqa: ARG005
    )
    assert added == []


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_removed_charger_entities(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord._devices_inventory_ready = True
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=DummyCatalogManager(_catalog_payload()),
        evse_firmware_details=DummyEvseFirmwareManager(_evse_payload()),
    )
    ent_reg = er.async_get(hass)
    removed_unique_id = _charger_update_unique_id("REMOVED123")
    ent_reg.async_get_or_create(
        "update",
        "enphase_ev",
        removed_unique_id,
        config_entry=config_entry,
    )

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: None,  # noqa: ARG005
    )

    assert (
        ent_reg.async_get_entity_id("update", "enphase_ev", removed_unique_id) is None
    )


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_charger_entities_when_type_unavailable(
    hass, config_entry
) -> None:
    coord = DummyCoordinator()
    coord._available_types = set()
    coord._devices_inventory_ready = True
    coord._active_evse_serials = []
    config_entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=DummyCatalogManager(_catalog_payload()),
        evse_firmware_details=DummyEvseFirmwareManager(_evse_payload()),
    )
    ent_reg = er.async_get(hass)
    removed_unique_id = _charger_update_unique_id(TEST_EVSE_SERIAL)
    ent_reg.async_get_or_create(
        "update",
        "enphase_ev",
        removed_unique_id,
        config_entry=config_entry,
    )

    await async_setup_entry(
        hass,
        config_entry,
        lambda entities, update_before_add=False: None,  # noqa: ARG005
    )

    assert (
        ent_reg.async_get_entity_id("update", "enphase_ev", removed_unique_id) is None
    )


@pytest.mark.asyncio
async def test_firmware_version_history_store_records_bounded_changes(
    hass, monkeypatch
) -> None:
    store = FirmwareVersionHistoryStore(hass, max_entries=2)
    monkeypatch.setattr(
        getattr(store, "_store"),
        "async_delay_save",
        lambda _data_func, _delay: None,
    )
    await store.async_load()
    assert store.history_for(None) == []
    assert (
        store.record_installed_version(
            unique_id=None,
            version="1.0",
            hass=hass,
            entity_id="update.gateway_firmware",
            name="Gateway Firmware",
        )
        == []
    )
    assert (
        store.record_installed_version(
            unique_id="update-1",
            version=None,
            hass=hass,
            entity_id="update.gateway_firmware",
            name="Gateway Firmware",
        )
        == []
    )

    times = iter(
        [
            "2026-07-01T00:00:00+00:00",
            "2026-07-02T00:00:00+00:00",
            "2026-07-03T00:00:00+00:00",
            "2026-07-04T00:00:00+00:00",
        ]
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.update._utc_now_iso",
        lambda: next(times),
    )
    log_entry = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.update.logbook.async_log_entry",
        log_entry,
    )

    first = store.record_installed_version(
        unique_id="update-1",
        version="1.0",
        hass=hass,
        entity_id="update.gateway_firmware",
        name="Gateway Firmware",
    )
    assert first == [
        {
            "version": "1.0",
            "first_seen_utc": "2026-07-01T00:00:00+00:00",
            "last_seen_utc": None,
        }
    ]
    log_entry.assert_not_called()

    assert (
        store.record_installed_version(
            unique_id="update-1",
            version="1.0",
            hass=hass,
            entity_id="update.gateway_firmware",
            name="Gateway Firmware",
        )
        == first
    )

    store.record_installed_version(
        unique_id="update-1",
        version="2.0",
        hass=hass,
        entity_id="update.gateway_firmware",
        name="Gateway Firmware",
    )
    store.record_installed_version(
        unique_id="update-1",
        version="3.0",
        hass=hass,
        entity_id="update.gateway_firmware",
        name="Gateway Firmware",
    )

    assert store.history_for("update-1") == [
        {
            "version": "2.0",
            "first_seen_utc": "2026-07-02T00:00:00+00:00",
            "last_seen_utc": "2026-07-03T00:00:00+00:00",
        },
        {
            "version": "3.0",
            "first_seen_utc": "2026-07-03T00:00:00+00:00",
            "last_seen_utc": None,
        },
    ]
    assert log_entry.call_count == 2
    log_entry.assert_called_with(
        hass,
        name="Gateway Firmware",
        message="installed firmware changed from 2.0 to 3.0",
        domain=DOMAIN,
        entity_id="update.gateway_firmware",
    )

    store.remove("update-1")
    assert store.history_for("update-1") == []


@pytest.mark.asyncio
async def test_firmware_version_history_store_loads_clean_entries(
    hass, monkeypatch
) -> None:
    store = FirmwareVersionHistoryStore(hass, max_entries=2)
    backing_store = getattr(store, "_store")
    monkeypatch.setattr(
        backing_store,
        "async_load",
        AsyncMock(
            return_value={
                "entries": {
                    "update-1": [
                        "bad",
                        {"version": "1.0"},
                        {
                            "version": "2.0",
                            "first_seen_utc": "2026-07-02T00:00:00+00:00",
                            "last_seen_utc": "2026-07-03T00:00:00+00:00",
                        },
                        {
                            "version": "3.0",
                            "first_seen_utc": "2026-07-03T00:00:00+00:00",
                        },
                    ],
                    "update-empty": [],
                }
            },
        ),
    )

    await store.async_load()

    assert store.history_for("update-1") == [
        {
            "version": "2.0",
            "first_seen_utc": "2026-07-02T00:00:00+00:00",
            "last_seen_utc": "2026-07-03T00:00:00+00:00",
        },
        {
            "version": "3.0",
            "first_seen_utc": "2026-07-03T00:00:00+00:00",
            "last_seen_utc": None,
        },
    ]
    assert store.history_for("update-empty") == []

    monkeypatch.setattr(backing_store, "async_load", AsyncMock(return_value=[]))
    await store.async_load()
    assert store.history_for("update-1") == []

    monkeypatch.setattr(
        backing_store, "async_load", AsyncMock(return_value={"entries": []})
    )
    await store.async_load()
    assert store.history_for("update-1") == []

    monkeypatch.setattr(
        backing_store,
        "async_load",
        AsyncMock(return_value={"entries": {"bad": "not-list"}}),
    )
    await store.async_load()
    assert store.history_for("bad") == []


@pytest.mark.asyncio
async def test_gateway_update_entity_states_and_release_url_selection(hass) -> None:
    coord = DummyCoordinator()
    manager = DummyCatalogManager(_catalog_payload())

    entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    entity.hass = hass

    entity._refresh_from_catalog(manager.cached_catalog)
    assert entity.installed_version == "8.2.4300"
    assert entity.latest_version == "8.2.4401"
    assert entity.state == "on"
    assert entity.supported_features == 0
    assert entity.release_url == "https://example.test/envoy/fr"
    assert entity.device_info["name"] == "envoy device"

    entity.async_write_ha_state = lambda: None
    await entity.async_skip()
    assert entity.state == "off"
    assert entity.state_attributes["skipped_version"] == "8.2.4401"
    await entity.async_clear_skipped()
    assert entity.state == "on"

    await entity.async_skip()
    coord._gateway_version = "8.2.4401"
    entity._refresh_from_catalog(manager.cached_catalog)
    assert entity.state == "off"
    assert entity.latest_version == "8.2.4401"
    assert entity.release_url == "https://example.test/envoy/fr"
    assert entity.release_summary == "Gateway firmware update"
    assert entity.state_attributes["release_url"] == "https://example.test/envoy/fr"
    assert entity.state_attributes["skipped_version"] is None

    coord._gateway_version = "8.3.5286"
    entity._refresh_from_catalog(manager.cached_catalog)
    assert entity.installed_version == "8.3.5286"
    assert entity.latest_version == "8.3.5286"
    assert entity.state == "off"
    assert entity.release_url is None
    assert entity.release_summary is None

    regional_payload = _catalog_payload()
    regional_payload["devices"]["envoy"]["latest_by_locale"]["fr-fr"] = {
        "version": "8.3.5232",
        "summary": "Gateway regional",
        "urls_by_locale": {"fr-fr": "https://example.test/envoy/fr-regional"},
    }
    regional_payload["devices"]["envoy"]["latest_by_country"]["AU"] = {
        "version": "8.3.5232",
        "summary": "Gateway regional",
        "urls_by_locale": {
            "fr-fr": "https://example.test/envoy/fr-regional",
            "en-au": "https://example.test/envoy/au-regional",
        },
    }
    regional_payload["devices"]["envoy"]["latest_global"] = {
        "version": "8.3.5427",
        "summary": "Gateway global",
        "urls_by_locale": {"en": "https://example.test/envoy/global-newer"},
    }
    coord._gateway_version = "8.3.5000"
    entity._refresh_from_catalog(regional_payload)
    assert entity.latest_version == "8.3.5232"
    assert entity.release_url == "https://example.test/envoy/fr-regional"
    assert entity.release_summary == "Gateway regional"

    coord._gateway_version = "8.3.5300"
    entity._refresh_from_catalog(regional_payload)
    assert entity.latest_version == "8.3.5300"
    assert entity.release_url is None
    assert entity.release_summary is None

    coord.battery_locale = "en-AU"
    coord._gateway_version = "8.3.5232"
    regional_payload["devices"]["envoy"]["latest_by_locale"]["en-au"] = {
        "version": "8.3.5427",
        "summary": "Gateway non-regional",
        "urls_by_locale": {
            "en": "https://example.test/envoy/global-newer",
            "en-au": "https://example.test/envoy/global-au",
        },
    }
    entity._refresh_from_catalog(regional_payload)
    assert entity.latest_version == "8.3.5232"
    assert entity.release_url == "https://example.test/envoy/au-regional"
    assert entity.release_summary == "Gateway regional"
    assert entity.extra_state_attributes["catalog_source_scope"] == "country"

    coord.battery_country_code = None
    hass.config.country = None
    entity._refresh_from_catalog(regional_payload)
    assert entity.latest_version is None
    assert entity.release_url is None
    assert entity.extra_state_attributes["catalog_source_scope"] is None

    coord.battery_country_code = "AU"
    coord.battery_locale = "fr-fr"

    coord._gateway_version = "8.2.4300"
    entity._refresh_from_catalog(manager.cached_catalog)
    await entity.async_skip()
    payload = _catalog_payload()
    payload["devices"]["envoy"]["latest_by_country"]["AU"]["version"] = "8.2.4500"
    payload["devices"]["envoy"]["latest_by_locale"]["fr-fr"]["version"] = "8.2.4500"
    entity._refresh_from_catalog(payload)
    assert entity.state == "on"
    assert entity.state_attributes["skipped_version"] is None

    coord._gateway_version = "firmware build unknown"
    entity._refresh_from_catalog(manager.cached_catalog)
    assert entity.latest_version is None
    assert entity.state is None
    coord._available_types = set()
    assert entity.available is False


@pytest.mark.asyncio
async def test_gateway_update_entity_exposes_version_history_and_logs_changes(
    hass, monkeypatch
) -> None:
    coord = DummyCoordinator()
    manager = DummyCatalogManager(_catalog_payload())
    history = FirmwareVersionHistoryStore(hass)
    monkeypatch.setattr(
        getattr(history, "_store"),
        "async_delay_save",
        lambda _data_func, _delay: None,
    )
    await history.async_load()
    times = iter(
        [
            "2026-07-01T00:00:00+00:00",
            "2026-07-02T00:00:00+00:00",
        ]
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.update._utc_now_iso",
        lambda: next(times),
    )
    log_entry = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.update.logbook.async_log_entry",
        log_entry,
    )

    entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
        version_history=history,
    )
    entity.hass = hass
    entity.entity_id = "update.gateway_firmware"

    entity._refresh_from_catalog(manager.cached_catalog)
    assert entity.extra_state_attributes["installed_version_history"] == [
        {
            "version": "8.2.4300",
            "first_seen_utc": "2026-07-01T00:00:00+00:00",
            "last_seen_utc": None,
        }
    ]
    log_entry.assert_not_called()

    coord._gateway_version = "8.2.4401"
    entity._refresh_from_catalog(manager.cached_catalog)

    assert entity.extra_state_attributes["installed_version_history"] == [
        {
            "version": "8.2.4300",
            "first_seen_utc": "2026-07-01T00:00:00+00:00",
            "last_seen_utc": "2026-07-02T00:00:00+00:00",
        },
        {
            "version": "8.2.4401",
            "first_seen_utc": "2026-07-02T00:00:00+00:00",
            "last_seen_utc": None,
        },
    ]
    log_entry.assert_called_once_with(
        hass,
        name="update.gateway_firmware",
        message="installed firmware changed from 8.2.4300 to 8.2.4401",
        domain=DOMAIN,
        entity_id="update.gateway_firmware",
    )


@pytest.mark.asyncio
async def test_gateway_update_entity_rejects_install_requests(hass) -> None:
    entity = FirmwareUpdateEntity(
        coordinator=DummyCoordinator(),
        manager=DummyCatalogManager(_catalog_payload()),
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    entity.hass = hass

    with pytest.raises(HomeAssistantError, match="advisory only"):
        await entity.async_install(version=None, backup=False)


def test_firmware_update_entities_fall_back_to_type_device_identifiers() -> None:
    coord = DummyCoordinator()
    coord.inventory_view = SimpleNamespace(
        has_type_for_entities=coord.has_type_for_entities,
        has_type=coord.has_type,
        type_bucket=coord.type_bucket,
        type_device_info=lambda _type: None,
        type_device_sw_version=coord.type_device_sw_version,
    )
    manager = DummyCatalogManager(_catalog_payload())

    gateway = FirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    assert gateway.device_info == {
        "identifiers": {(DOMAIN, f"type:{coord.site_id}:envoy")},
        "manufacturer": "Enphase",
    }


@pytest.mark.asyncio
async def test_charger_update_entity_uses_fw_details_payload(hass) -> None:
    coord = DummyCoordinator()
    manager = DummyEvseFirmwareManager(_evse_payload())
    catalog_manager = DummyCatalogManager(_catalog_payload())
    entity = ChargerFirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        catalog_manager=catalog_manager,
        serial=TEST_EVSE_SERIAL,
        description=UpdateEntityDescription(key="charger_firmware"),
    )
    entity.hass = hass

    entity._refresh_from_details(manager.cached_details)
    entity._refresh_from_catalog(catalog_manager.cached_catalog)
    assert entity.installed_version == "25.37.1.13"
    assert entity.latest_version == "25.37.1.14"
    assert entity.state == "on"
    assert entity.available is True
    assert entity.supported_features == 0
    assert entity.device_info["identifiers"] == {("enphase_ev", TEST_EVSE_SERIAL)}
    assert entity.release_url == "https://example.test/charger/fr"
    assert entity.release_summary == "Charger firmware update"

    attrs = entity.extra_state_attributes
    assert attrs["upgrade_status"] == 5
    assert attrs["last_successful_upgrade_date"] == (
        "2025-12-08T22:41:46.568837098Z[UTC]"
    )
    assert attrs["last_updated_at"] == "2025-12-08T15:52:59.806385175Z[UTC]"
    assert attrs["is_auto_ota"] is False
    assert attrs["firmware_rollout_enabled"] is True
    assert attrs["details_last_error"] is None
    assert attrs["catalog_source_scope"] == "country"
    assert attrs["catalog_generated_at"] == "2026-03-01T00:00:00Z"

    entity.async_write_ha_state = lambda: None
    await entity.async_skip()
    assert entity.state == "off"
    assert entity.state_attributes["skipped_version"] == "25.37.1.14"
    await entity.async_clear_skipped()
    assert entity.state == "on"

    await entity.async_skip()
    manager.cached_details[TEST_EVSE_SERIAL]["currentFwVersion"] = "25.37.1.14"
    entity._refresh_from_details(manager.cached_details)
    assert entity.state == "off"
    assert entity.latest_version == "25.37.1.14"
    assert entity.release_url == "https://example.test/charger/fr"
    assert entity.release_summary == "Charger firmware update"
    assert entity.state_attributes["release_url"] == "https://example.test/charger/fr"
    assert entity.state_attributes["skipped_version"] is None

    manager.cached_details[TEST_EVSE_SERIAL]["currentFwVersion"] = "25.37.1.15"
    manager.cached_details[TEST_EVSE_SERIAL]["targetFwVersion"] = "25.37.1.14"
    entity._refresh_from_details(manager.cached_details)
    assert entity.installed_version == "25.37.1.15"
    assert entity.latest_version == "25.37.1.15"
    assert entity.state == "off"
    assert entity.release_url is None
    assert entity.release_summary is None

    manager.cached_details[TEST_EVSE_SERIAL]["currentFwVersion"] = "25.37.1.13"
    manager.cached_details[TEST_EVSE_SERIAL]["targetFwVersion"] = "25.37.1.14"
    entity._refresh_from_details(manager.cached_details)
    entity._refresh_from_catalog(catalog_manager.cached_catalog)
    await entity.async_skip()
    manager.cached_details[TEST_EVSE_SERIAL]["currentFwVersion"] = "25.37.1.13"
    manager.cached_details[TEST_EVSE_SERIAL]["targetFwVersion"] = "25.37.1.15"
    entity._refresh_from_details(manager.cached_details)
    assert entity.state == "on"
    assert entity.state_attributes["skipped_version"] is None


@pytest.mark.asyncio
async def test_charger_update_entity_exposes_version_history(hass, monkeypatch) -> None:
    coord = DummyCoordinator()
    manager = DummyEvseFirmwareManager(_evse_payload())
    history = FirmwareVersionHistoryStore(hass)
    monkeypatch.setattr(
        getattr(history, "_store"),
        "async_delay_save",
        lambda _data_func, _delay: None,
    )
    await history.async_load()
    times = iter(
        [
            "2026-07-01T00:00:00+00:00",
            "2026-07-02T00:00:00+00:00",
        ]
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.update._utc_now_iso",
        lambda: next(times),
    )
    log_entry = MagicMock()
    monkeypatch.setattr(
        "custom_components.enphase_ev.update.logbook.async_log_entry",
        log_entry,
    )
    entity = ChargerFirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        catalog_manager=DummyCatalogManager(_catalog_payload()),
        serial=TEST_EVSE_SERIAL,
        description=UpdateEntityDescription(key="charger_firmware"),
        version_history=history,
    )
    entity.hass = hass
    entity.entity_id = "update.driveway_charger_firmware"

    entity._refresh_from_details(manager.cached_details)
    assert entity.extra_state_attributes["installed_version_history"] == [
        {
            "version": "25.37.1.13",
            "first_seen_utc": "2026-07-01T00:00:00+00:00",
            "last_seen_utc": None,
        }
    ]

    manager.cached_details[TEST_EVSE_SERIAL]["currentFwVersion"] = "25.37.1.14"
    entity._refresh_from_details(manager.cached_details)

    assert entity.extra_state_attributes["installed_version_history"] == [
        {
            "version": "25.37.1.13",
            "first_seen_utc": "2026-07-01T00:00:00+00:00",
            "last_seen_utc": "2026-07-02T00:00:00+00:00",
        },
        {
            "version": "25.37.1.14",
            "first_seen_utc": "2026-07-02T00:00:00+00:00",
            "last_seen_utc": None,
        },
    ]
    log_entry.assert_called_once_with(
        hass,
        name="update.driveway_charger_firmware",
        message="installed firmware changed from 25.37.1.13 to 25.37.1.14",
        domain=DOMAIN,
        entity_id="update.driveway_charger_firmware",
    )


@pytest.mark.asyncio
async def test_charger_update_entity_falls_back_to_summary_versions(hass) -> None:
    coord = DummyCoordinator()
    manager = DummyEvseFirmwareManager(
        {
            TEST_EVSE_SERIAL: {
                "serialNumber": TEST_EVSE_SERIAL,
                "targetFwVersion": "25.37.1.14",
            }
        }
    )
    entity = ChargerFirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        catalog_manager=DummyCatalogManager(_catalog_payload()),
        serial=TEST_EVSE_SERIAL,
        description=UpdateEntityDescription(key="charger_firmware"),
    )
    entity.hass = hass

    entity._refresh_from_details(manager.cached_details)
    assert entity.installed_version == "25.37.1.13"
    assert entity.latest_version == "25.37.1.14"
    assert entity.state == "on"

    manager.cached_details[TEST_EVSE_SERIAL]["targetFwVersion"] = "firmware pending"
    entity._refresh_from_details(manager.cached_details)
    assert entity.latest_version is None
    assert entity.state is None


@pytest.mark.asyncio
async def test_charger_update_entity_suppresses_notification_when_rollout_disabled(
    hass,
) -> None:
    coord = DummyCoordinator()
    coord._evse_feature_flags[TEST_EVSE_SERIAL]["iqevse_itk_fw_upgrade_status"] = False
    manager = DummyEvseFirmwareManager(_evse_payload())
    entity = ChargerFirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        catalog_manager=DummyCatalogManager(_catalog_payload()),
        serial=TEST_EVSE_SERIAL,
        description=UpdateEntityDescription(key="charger_firmware"),
    )
    entity.hass = hass

    entity._refresh_from_details(manager.cached_details)
    assert entity.latest_version == "25.37.1.14"
    assert entity.state == "off"
    assert entity.extra_state_attributes["firmware_rollout_enabled"] is False


@pytest.mark.asyncio
async def test_charger_update_entity_clears_release_metadata_without_catalog_entry(
    hass,
) -> None:
    coord = DummyCoordinator()
    manager = DummyEvseFirmwareManager(_evse_payload())
    entity = ChargerFirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        catalog_manager=DummyCatalogManager({"generated_at": "2026-03-01T00:00:00Z"}),
        serial=TEST_EVSE_SERIAL,
        description=UpdateEntityDescription(key="charger_firmware"),
    )
    entity.hass = hass
    entity._attr_release_url = "https://example.test/old"
    entity._attr_release_summary = "Old"

    entity._refresh_from_catalog({"generated_at": "2026-03-01T00:00:00Z"})
    assert entity.release_url is None
    assert entity.release_summary is None
    assert (
        entity.extra_state_attributes["catalog_generated_at"] == "2026-03-01T00:00:00Z"
    )


@pytest.mark.asyncio
async def test_charger_update_entity_rejects_install_requests(hass) -> None:
    entity = ChargerFirmwareUpdateEntity(
        coordinator=DummyCoordinator(),
        manager=DummyEvseFirmwareManager(_evse_payload()),
        catalog_manager=DummyCatalogManager(_catalog_payload()),
        serial=TEST_EVSE_SERIAL,
        description=UpdateEntityDescription(key="charger_firmware"),
    )
    entity.hass = hass

    with pytest.raises(HomeAssistantError, match="advisory only"):
        await entity.async_install(version=None, backup=False)


@pytest.mark.asyncio
async def test_entity_refresh_and_scheduler_branches(hass, monkeypatch) -> None:
    coord = DummyCoordinator()
    catalog_manager = DummyCatalogManager(_catalog_payload())
    entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=catalog_manager,
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    entity.hass = hass

    monkeypatch.setattr(
        CoordinatorEntity,
        "async_added_to_hass",
        AsyncMock(return_value=None),
    )
    remove = AsyncMock(return_value=None)
    monkeypatch.setattr(CoordinatorEntity, "async_will_remove_from_hass", remove)
    monkeypatch.setattr(
        CoordinatorEntity, "_handle_coordinator_update", lambda self: None
    )
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)

    await entity.async_added_to_hass()

    running = hass.async_create_task(asyncio.sleep(0.2))
    entity._refresh_task = running
    entity._schedule_catalog_refresh()
    assert entity._refresh_task is running
    running.cancel()
    try:
        await running
    except asyncio.CancelledError:
        pass

    pending_removal = hass.async_create_task(asyncio.sleep(60))
    entity._refresh_task = pending_removal
    await entity.async_will_remove_from_hass()
    with pytest.raises(asyncio.CancelledError):
        await pending_removal
    assert pending_removal.cancelled()
    assert entity._refresh_task is None
    remove.assert_awaited_once_with()

    entity.hass = None
    entity._schedule_catalog_refresh()
    entity.hass = hass

    entity._handle_coordinator_update()

    class _FailingManager(DummyCatalogManager):
        async def async_get_catalog(
            self, *, force_refresh: bool = False
        ):  # noqa: ARG002
            raise RuntimeError("boom")

    failing_entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=_FailingManager(_catalog_payload()),
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    failing_entity.hass = hass
    await failing_entity._async_refresh_catalog()


@pytest.mark.asyncio
async def test_charger_entity_refresh_branches(hass, monkeypatch) -> None:
    coord = DummyCoordinator()
    manager = DummyEvseFirmwareManager(_evse_payload())
    catalog_manager = DummyCatalogManager(_catalog_payload())
    entity = ChargerFirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        catalog_manager=catalog_manager,
        serial=TEST_EVSE_SERIAL,
        description=UpdateEntityDescription(key="charger_firmware"),
    )
    entity.hass = hass

    monkeypatch.setattr(
        CoordinatorEntity,
        "async_added_to_hass",
        AsyncMock(return_value=None),
    )
    remove = AsyncMock(return_value=None)
    monkeypatch.setattr(CoordinatorEntity, "async_will_remove_from_hass", remove)
    monkeypatch.setattr(
        CoordinatorEntity, "_handle_coordinator_update", lambda self: None
    )
    write_state = MagicMock()
    monkeypatch.setattr(entity, "async_write_ha_state", write_state)

    await entity.async_added_to_hass()

    running = hass.async_create_task(asyncio.sleep(0.2))
    entity._refresh_task = running
    entity._schedule_details_refresh()
    assert entity._refresh_task is running
    running.cancel()
    try:
        await running
    except asyncio.CancelledError:
        pass

    pending_removal = hass.async_create_task(asyncio.sleep(60))
    entity._refresh_task = pending_removal
    await entity.async_will_remove_from_hass()
    with pytest.raises(asyncio.CancelledError):
        await pending_removal
    assert pending_removal.cancelled()
    assert entity._refresh_task is None
    remove.assert_awaited_once_with()

    entity.hass = None
    entity._schedule_details_refresh()
    entity.hass = hass
    entity.entity_id = "update.test_charger_firmware"
    await entity._async_refresh_state()
    write_state.assert_called()
    entity._handle_coordinator_update()

    class _FailingManager(DummyEvseFirmwareManager):
        async def async_get_details(
            self, *, force_refresh: bool = False
        ):  # noqa: ARG002
            raise RuntimeError("boom")

    failing_entity = ChargerFirmwareUpdateEntity(
        coordinator=coord,
        manager=_FailingManager(_evse_payload()),
        catalog_manager=catalog_manager,
        serial=TEST_EVSE_SERIAL,
        description=UpdateEntityDescription(key="charger_firmware"),
    )
    failing_entity.hass = hass
    await failing_entity._async_refresh_state()

    class _FailingCatalogManager(DummyCatalogManager):
        async def async_get_catalog(
            self, *, force_refresh: bool = False
        ):  # noqa: ARG002
            raise RuntimeError("boom")

    failing_catalog_entity = ChargerFirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        catalog_manager=_FailingCatalogManager(_catalog_payload()),
        serial=TEST_EVSE_SERIAL,
        description=UpdateEntityDescription(key="charger_firmware"),
    )
    failing_catalog_entity.hass = hass
    await failing_catalog_entity._async_refresh_state()


@pytest.mark.asyncio
async def test_refresh_from_catalog_none_and_locale_fallback_paths(hass) -> None:
    coord = DummyCoordinator()
    coord.battery_locale = "es-es"
    payload = _catalog_payload()
    payload["devices"]["envoy"]["latest_by_country"] = {}
    payload["devices"]["envoy"]["latest_global"] = {
        "version": "8.2.4401",
        "countries_text": "Worldwide",
        "summary": "Global only",
        "urls_by_locale": {
            "de-de": "https://example.test/envoy/de",
            "fr-fr": "https://example.test/envoy/fr",
        },
    }
    manager = DummyCatalogManager(payload)
    entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=manager,
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    entity.hass = hass

    entity._refresh_from_catalog(None)
    assert entity.latest_version is None
    assert entity.release_url is None
    assert entity.release_summary is None

    entity._refresh_from_catalog(payload)
    assert entity.release_url == "https://example.test/envoy/de"
    assert entity.extra_state_attributes["catalog_source_scope"] == "global"


def test_helper_functions_cover_edge_paths() -> None:
    assert (
        _type_available(
            SimpleNamespace(
                inventory_view=SimpleNamespace(has_type_for_entities=lambda _key: True)
            ),
            "envoy",
        )
        is True
    )
    assert _type_available(
        SimpleNamespace(
            inventory_view=SimpleNamespace(
                has_type_for_entities=lambda key: key == "envoy"
            )
        ),
        "envoy",
    )
    assert not _type_available(
        SimpleNamespace(
            inventory_view=SimpleNamespace(has_type_for_entities=lambda key: False)
        ),
        "envoy",
    )

    assert (
        _gateway_installed_version(
            SimpleNamespace(
                inventory_view=SimpleNamespace(
                    type_device_sw_version=lambda _type: None
                )
            )
        )
        is None
    )

    coord = SimpleNamespace(
        data={"SN1": {"firmware_version": "1.2.3"}},
        inventory_view=SimpleNamespace(
            type_device_sw_version=lambda _type: None,
            type_bucket=lambda _type: None,
        ),
    )
    assert _charger_serials(SimpleNamespace()) == []
    assert _charger_installed_version(coord, "SN1") == "1.2.3"
    fallback_coord = SimpleNamespace(
        inventory_view=SimpleNamespace(type_device_sw_version=lambda _type: "2.0")
    )
    assert _charger_installed_version(fallback_coord, "SN2") == "2.0"
    assert (
        _charger_installed_version(
            SimpleNamespace(
                inventory_view=SimpleNamespace(
                    type_device_sw_version=lambda _type: None
                )
            ),
            "SN3",
        )
        is None
    )
    assert _as_bool(True) is True
    assert _as_bool(1) is True
    assert _as_bool("0") is False
    assert _as_bool("yes") is True
    assert _as_bool("unknown") is None
    assert _as_int("5") == 5
    assert _as_int("bad") is None
    assert _evse_firmware_rollout_enabled(SimpleNamespace(), "SN1") is None
    assert (
        _evse_firmware_rollout_enabled(
            SimpleNamespace(evse_feature_flag_enabled=lambda key, sn: key == "x"), "SN1"
        )
        is False
    )
    assert (
        _evse_firmware_rollout_enabled(
            SimpleNamespace(
                evse_feature_flag_enabled=lambda _key, _sn: (_ for _ in ()).throw(
                    RuntimeError("boom")
                )
            ),
            "SN1",
        )
        is None
    )
    assert _text(None) is None

    class _BadStr:
        def __str__(self):
            raise ValueError("boom")

    assert _text(_BadStr()) is None
    assert _as_bool(_BadStr()) is None


def test_charger_installed_version_ignores_mixed_iqevse_inventory_summary() -> None:
    from custom_components.enphase_ev.inventory_view import InventoryView

    coord = SimpleNamespace(
        site_id=TEST_EVSE_SITE_ID,
        data={},
        _type_device_buckets={
            "iqevse": {
                "type_key": "iqevse",
                "type_label": "EV Chargers",
                "count": 2,
                "devices": [
                    {"serial_number": "SN1", "sw_version": "1.0"},
                    {"serial_number": "SN2", "sw_version": "2.0"},
                ],
            }
        },
        _type_device_order=["iqevse"],
        _selected_type_keys=None,
    )
    coord.inventory_view = InventoryView(coord)

    assert coord.inventory_view.type_device_sw_version("iqevse") is None
    assert coord.inventory_view.type_device_sw_version_summary("iqevse") is None
    assert _charger_installed_version(coord, "SN1") is None


def test_device_info_falls_back_when_coordinator_does_not_supply_info() -> None:
    coord = DummyCoordinator()
    coord.inventory_view.type_device_info = lambda _type: None
    entity = FirmwareUpdateEntity(
        coordinator=coord,
        manager=DummyCatalogManager(_catalog_payload()),
        device_type="envoy",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    assert entity.device_info == {
        "identifiers": {(DOMAIN, f"type:{coord.site_id}:envoy")},
        "manufacturer": "Enphase",
    }

    other = FirmwareUpdateEntity(
        coordinator=coord,
        manager=DummyCatalogManager(_catalog_payload()),
        device_type="other",
        translation_key="gateway_firmware",
        description=UpdateEntityDescription(key="gateway_firmware"),
        installed_version_getter=_gateway_installed_version,
    )
    assert other.device_info is None


def test_prune_removed_charger_updates_covers_registry_filters() -> None:
    removed: list[str] = []
    removed_history: list[str] = []
    ent_reg = SimpleNamespace(
        entities={
            "no_domain": SimpleNamespace(
                entity_id="update.no_domain",
                domain=None,
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id=_charger_update_unique_id("REMOVE_ME"),
            ),
            "wrong_domain": SimpleNamespace(
                entity_id="sensor.wrong_domain",
                domain="sensor",
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id=_charger_update_unique_id("SENSOR_SKIP"),
            ),
            "wrong_platform": SimpleNamespace(
                entity_id="update.wrong_platform",
                domain="update",
                platform="other_platform",
                config_entry_id="entry-1",
                unique_id=_charger_update_unique_id("PLATFORM_SKIP"),
            ),
            "wrong_entry": SimpleNamespace(
                entity_id="update.wrong_entry",
                domain="update",
                platform=DOMAIN,
                config_entry_id="entry-2",
                unique_id=_charger_update_unique_id("ENTRY_SKIP"),
            ),
            "bad_unique": SimpleNamespace(
                entity_id="update.bad_unique",
                domain="update",
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id="not_a_charger_update",
            ),
            "current_serial": SimpleNamespace(
                entity_id="update.current_serial",
                domain="update",
                platform=DOMAIN,
                config_entry_id="entry-1",
                unique_id=_charger_update_unique_id("KEEP_ME"),
            ),
        },
        async_remove=lambda entity_id: removed.append(entity_id),
    )
    known_serials = {"REMOVE_ME", "KEEP_ME"}

    _async_prune_removed_charger_updates(
        entry=SimpleNamespace(entry_id="entry-1"),
        ent_reg=ent_reg,
        current_serials={"KEEP_ME"},
        known_serials=known_serials,
        version_history=SimpleNamespace(remove=removed_history.append),
    )

    assert removed == ["update.no_domain"]
    assert removed_history == [_charger_update_unique_id("REMOVE_ME")]
    assert known_serials == {"KEEP_ME"}
