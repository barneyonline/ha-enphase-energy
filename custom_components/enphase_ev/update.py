from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar, cast

from homeassistant.components import logbook
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
)
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, callback as ha_callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .evse_firmware import EvseFirmwareDetailsManager
from .firmware_catalog import (
    FirmwareCatalogManager,
    compare_versions,
    normalize_locale,
    normalize_version_token,
    resolve_country_and_locale,
    select_catalog_entry,
)
from .gateway_software_update import GatewaySoftwareUpdateManager
from .log_redaction import redact_identifier, redact_text
from .parsing_helpers import coerce_optional_text as _text
from .runtime_helpers import (
    inventory_type_available as _type_available,
    inventory_type_device_info as _type_device_info,
)
from .runtime_data import EnphaseConfigEntry, get_runtime_data
from .serial_discovery import active_charger_serials_for_cleanup

PARALLEL_UPDATES = 0
FIRMWARE_HISTORY_STORAGE_KEY = f"{DOMAIN}_firmware_version_history"
FIRMWARE_HISTORY_STORAGE_VERSION = 1
FIRMWARE_HISTORY_MAX_ENTRIES = 10
FIRMWARE_HISTORY_SAVE_DELAY = 5
FIRMWARE_HISTORY_MANAGER_DATA_KEY = f"{DOMAIN}_firmware_version_history_manager"

_LOGGER = logging.getLogger(__name__)

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])
callback = cast(Callable[[_CallbackT], _CallbackT], ha_callback)


class FirmwareVersionHistoryStore:
    """Persist bounded installed firmware version transitions."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        max_entries: int = FIRMWARE_HISTORY_MAX_ENTRIES,
    ) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass,
            FIRMWARE_HISTORY_STORAGE_VERSION,
            FIRMWARE_HISTORY_STORAGE_KEY,
            private=True,
        )
        self._history: dict[str, list[dict[str, str | None]]] = {}
        self._max_entries = max(1, max_entries)
        self._load_lock = asyncio.Lock()
        self._loaded = False

    async def async_load(self) -> None:
        """Load stored firmware history."""
        stored = await self._store.async_load()
        if not isinstance(stored, dict):
            self._history = {}
            self._loaded = True
            return

        entries_by_id = stored.get("entries")
        if not isinstance(entries_by_id, dict):
            self._history = {}
            self._loaded = True
            return

        history: dict[str, list[dict[str, str | None]]] = {}
        for unique_id, entries in entries_by_id.items():
            clean = self._clean_entries(entries)
            if clean:
                history[str(unique_id)] = clean
        self._history = history
        self._loaded = True

    async def async_ensure_loaded(self) -> None:
        """Load history once across all concurrently setting-up config entries."""

        if self._loaded:
            return
        async with self._load_lock:
            if not self._loaded:
                await self.async_load()
                self._loaded = True

    def history_for(self, unique_id: str | None) -> list[dict[str, str | None]]:
        """Return a copy of the stored history for an update entity."""
        if not unique_id:
            return []
        return [dict(entry) for entry in self._history.get(unique_id, [])]

    def record_installed_version(
        self,
        *,
        unique_id: str | None,
        version: str | None,
        hass: HomeAssistant | None,
        entity_id: str | None,
        name: str | None,
    ) -> list[dict[str, str | None]]:
        """Record a version transition and log user-visible changes."""
        if not unique_id or not version:
            return self.history_for(unique_id)

        history = self._history.setdefault(unique_id, [])
        current = history[-1] if history else None
        current_version = _text(current.get("version")) if current else None

        if current_version == version:
            return self.history_for(unique_id)

        now = _utc_now_iso()
        if current is not None:
            current["last_seen_utc"] = now
        history.append(
            {
                "version": version,
                "first_seen_utc": now,
                "last_seen_utc": None,
            }
        )
        del history[: max(0, len(history) - self._max_entries)]
        self._schedule_save()

        if current_version and hass is not None and entity_id is not None:
            logbook.async_log_entry(
                hass,
                name=name or entity_id,
                message=(
                    "installed firmware changed from " f"{current_version} to {version}"
                ),
                domain=DOMAIN,
                entity_id=entity_id,
            )

        return self.history_for(unique_id)

    def remove(self, unique_id: str) -> None:
        """Remove history for a retired update entity."""
        if unique_id not in self._history:
            return
        self._history.pop(unique_id)
        self._schedule_save()

    def _schedule_save(self) -> None:
        self._store.async_delay_save(
            lambda: {"entries": self._history},
            FIRMWARE_HISTORY_SAVE_DELAY,
        )

    def _clean_entries(self, entries: Any) -> list[dict[str, str | None]]:
        if not isinstance(entries, list):
            return []
        clean: list[dict[str, str | None]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            version = _text(entry.get("version"))
            first_seen = _text(entry.get("first_seen_utc"))
            last_seen = _text(entry.get("last_seen_utc"))
            if not version or not first_seen:
                continue
            clean.append(
                {
                    "version": version,
                    "first_seen_utc": first_seen,
                    "last_seen_utc": last_seen,
                }
            )
        return clean[-self._max_entries :]


async def async_get_firmware_version_history_store(
    hass: HomeAssistant,
) -> FirmwareVersionHistoryStore:
    """Return the Home Assistant-wide firmware history owner."""

    store = hass.data.get(FIRMWARE_HISTORY_MANAGER_DATA_KEY)
    if not isinstance(store, FirmwareVersionHistoryStore):
        store = FirmwareVersionHistoryStore(hass)
        hass.data[FIRMWARE_HISTORY_MANAGER_DATA_KEY] = store
    await store.async_ensure_loaded()
    return store


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime_data = get_runtime_data(entry)
    coord = runtime_data.coordinator
    catalog_manager = runtime_data.firmware_catalog or FirmwareCatalogManager(hass)
    evse_manager = runtime_data.evse_firmware_details or EvseFirmwareDetailsManager(
        lambda: coord.client
    )
    gateway_update_manager = (
        runtime_data.gateway_software_update
        or GatewaySoftwareUpdateManager(
            lambda: coord.client,
            lambda: coord.inventory_view.type_device_serial_number("envoy"),
        )
    )
    version_history = await async_get_firmware_version_history_store(hass)
    ent_reg = er.async_get(hass)

    entities: list[UpdateEntity] = []
    if _type_available(coord, "envoy"):
        entities.append(
            FirmwareUpdateEntity(
                coordinator=coord,
                manager=catalog_manager,
                device_type="envoy",
                translation_key="gateway_firmware",
                description=UpdateEntityDescription(
                    key="gateway_firmware",
                    device_class=UpdateDeviceClass.FIRMWARE,
                    entity_category=EntityCategory.DIAGNOSTIC,
                ),
                installed_version_getter=_gateway_installed_version,
                version_history=version_history,
                progress_manager=gateway_update_manager,
            )
        )

    if entities:
        async_add_entities(entities, update_before_add=False)

    known_serials: set[str] = set()

    @callback
    def _async_sync_charger_updates() -> None:
        active_serials = active_charger_serials_for_cleanup(coord)
        current_serials = (
            active_serials
            if active_serials is not None
            else (
                set(_charger_serials(coord))
                if _type_available(coord, "iqevse")
                else set()
            )
        )
        if active_serials is not None:
            _async_prune_removed_charger_updates(
                entry=entry,
                ent_reg=ent_reg,
                current_serials=current_serials,
                known_serials=known_serials,
                version_history=version_history,
            )
        if not current_serials and (
            active_serials is None and not _type_available(coord, "iqevse")
        ):
            return
        known_serials.intersection_update(current_serials)
        serials = [sn for sn in current_serials if sn and sn not in known_serials]
        if not serials:
            return
        charger_entities = [
            ChargerFirmwareUpdateEntity(
                coordinator=coord,
                manager=evse_manager,
                catalog_manager=catalog_manager,
                serial=sn,
                description=UpdateEntityDescription(
                    key="charger_firmware",
                    device_class=UpdateDeviceClass.FIRMWARE,
                    entity_category=EntityCategory.DIAGNOSTIC,
                ),
                version_history=version_history,
            )
            for sn in serials
        ]
        async_add_entities(charger_entities, update_before_add=False)
        known_serials.update(serials)

    _async_sync_charger_updates()
    add_listener = getattr(coord, "async_add_topology_listener", None)
    if not callable(add_listener):
        add_listener = getattr(coord, "async_add_listener", None)
    if callable(add_listener):
        unsubscribe = add_listener(_async_sync_charger_updates)
        entry.async_on_unload(unsubscribe)


class FirmwareUpdateEntity(CoordinatorEntity[EnphaseCoordinator], UpdateEntity):  # type: ignore[misc]
    _attr_has_entity_name = True

    def __init__(
        self,
        *,
        coordinator: EnphaseCoordinator,
        manager: FirmwareCatalogManager,
        device_type: str,
        translation_key: str,
        description: UpdateEntityDescription,
        installed_version_getter: Callable[[EnphaseCoordinator], str | None],
        version_history: FirmwareVersionHistoryStore | None = None,
        progress_manager: GatewaySoftwareUpdateManager | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._coord = coordinator
        self._manager = manager
        self._device_type = device_type
        self._installed_version_getter = installed_version_getter
        self._version_history = version_history
        self._progress_manager = progress_manager
        self._refresh_task = None
        self._progress_refresh_task = None

        self._attr_translation_key = translation_key
        self._attr_unique_id = (
            f"{DOMAIN}_site_{coordinator.site_id}_{device_type}_firmware"
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

        self._country_used: str | None = None
        self._locale_used: str = "en"
        self._source_scope: str | None = None
        self._raw_installed_version: str | None = None
        self._raw_latest_version: str | None = None
        self._catalog_generated_at: str | None = None
        self._installed_version_history: list[dict[str, str | None]] = []
        self._gateway_update_status: dict[str, Any] | None = None

        self._refresh_from_catalog(self._manager.cached_catalog)

    @property
    def available(self) -> bool:
        return cast(
            bool,
            super().available and _type_available(self._coord, self._device_type),
        )

    @property
    def device_info(self) -> DeviceInfo | None:
        info = _type_device_info(self._coord, self._device_type)
        if info is not None:
            return info
        if self._device_type == "envoy":
            return DeviceInfo(
                identifiers={(DOMAIN, f"type:{self._coord.site_id}:envoy")},
                manufacturer="Enphase",
            )
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._manager.status_snapshot()
        attributes = {
            "catalog_source_scope": self._source_scope,
            "catalog_generated_at": self._catalog_generated_at,
            "catalog_last_error": status.get("last_error"),
            "installed_version_history": self._installed_version_history,
        }
        if self._progress_manager is None:
            return attributes
        progress_cache = self._progress_manager.status_snapshot()
        attributes.update(
            {
                "software_update_last_fetch_utc": progress_cache.get("last_fetch_utc"),
                "software_update_last_success_utc": progress_cache.get(
                    "last_success_utc"
                ),
                "software_update_last_error": progress_cache.get("last_error"),
                "software_update_using_stale": progress_cache.get("using_stale"),
            }
        )
        if not isinstance(self._gateway_update_status, dict):
            return attributes
        progress_attributes = {
            "software_update_current_status": _status_value(
                self._gateway_update_status, "current_status_text"
            ),
            "software_update_current_status_code": _status_value(
                self._gateway_update_status, "current_status"
            ),
            "software_update_last_status": _status_value(
                self._gateway_update_status, "last_status_text"
            ),
            "software_update_last_status_code": _status_value(
                self._gateway_update_status, "last_status"
            ),
            "estimated_time_left": _status_value(
                self._gateway_update_status, "estimated_time_left"
            ),
            "estimated_time_left_seconds": _status_value(
                self._gateway_update_status, "estimated_time_left_seconds"
            ),
            "total_update_duration": _status_value(
                self._gateway_update_status, "total_duration"
            ),
            "total_update_duration_seconds": _status_value(
                self._gateway_update_status, "total_duration_seconds"
            ),
            "installed_image_version": _status_value(
                self._gateway_update_status, "installed_image_version"
            ),
            "software_update_last_reported_at": _status_value(
                self._gateway_update_status, "last_reported_at"
            ),
            "software_update_device_statuses": _status_value(
                self._gateway_update_status, "device_statuses"
            ),
            "software_update_components": _status_value(
                self._gateway_update_status, "component_updates"
            ),
            "software_update_e3_progress": _status_value(
                self._gateway_update_status, "e3_progress"
            ),
            "software_update_transfer_speed_bps": _status_value(
                self._gateway_update_status, "transfer_speed_bps"
            ),
        }
        attributes.update(
            {
                key: value
                for key, value in progress_attributes.items()
                if value is not None and value != []
            }
        )
        return attributes

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_refresh_catalog()
        self._schedule_progress_refresh()

    async def async_will_remove_from_hass(self) -> None:
        refresh_task = self._refresh_task
        self._refresh_task = None
        if refresh_task is not None and not refresh_task.done():
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass

        progress_refresh_task = self._progress_refresh_task
        self._progress_refresh_task = None
        if progress_refresh_task is not None and not progress_refresh_task.done():
            progress_refresh_task.cancel()
            try:
                await progress_refresh_task
            except asyncio.CancelledError:
                pass
        await super().async_will_remove_from_hass()

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Reject install requests; these entities only advertise firmware status."""
        raise HomeAssistantError(
            "Firmware updates are advisory only",
            translation_domain=DOMAIN,
            translation_key="firmware_advisory_only",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_from_catalog(self._manager.cached_catalog)
        if self._progress_manager is not None:
            self._apply_progress(self._progress_manager.cached_status)
        self._schedule_catalog_refresh()
        self._schedule_progress_refresh()
        super()._handle_coordinator_update()

    def _schedule_progress_refresh(self) -> None:
        if self.hass is None or self._progress_manager is None:
            return
        if (
            self._progress_refresh_task is not None
            and not self._progress_refresh_task.done()
        ):
            return
        self._progress_refresh_task = self.hass.async_create_background_task(
            self._async_refresh_progress_loop(),
            name=f"{DOMAIN}_gateway_software_update_progress",
        )

    async def _async_refresh_progress_loop(self) -> None:
        assert self._progress_manager is not None
        while True:
            update_status = await self._progress_manager.async_get_status()
            self._apply_progress(update_status)
            self.async_write_ha_state()
            await asyncio.sleep(self._progress_manager.next_refresh_seconds)

    def _apply_progress(self, status: dict[str, Any] | None) -> None:
        self._gateway_update_status = status
        self._attr_in_progress = cast(bool | None, _status_value(status, "in_progress"))
        self._attr_update_percentage = cast(
            int | float | None, _status_value(status, "update_percentage")
        )

    def _schedule_catalog_refresh(self) -> None:
        if self.hass is None:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = self.hass.async_create_task(
            self._async_refresh_catalog(),
            name=f"{DOMAIN}_firmware_catalog_refresh_{self._device_type}",
        )

    async def _async_refresh_catalog(self) -> None:
        try:
            catalog = await self._manager.async_get_catalog()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Firmware catalog refresh failed for %s: %s",
                self._device_type,
                redact_text(err),
            )
            return

        self._refresh_from_catalog(catalog)
        self.async_write_ha_state()

    def _refresh_from_catalog(self, catalog: dict[str, Any] | None) -> None:
        country, locale = resolve_country_and_locale(self._coord, self.hass)
        normalized_locale = normalize_locale(locale)

        self._country_used = country
        self._locale_used = normalized_locale

        raw_installed = self._installed_version_getter(self._coord)
        normalized_installed = normalize_version_token(raw_installed)
        self._raw_installed_version = raw_installed
        self._attr_installed_version = normalized_installed
        self._update_installed_version_history(normalized_installed)

        selected = select_catalog_entry(
            catalog,
            device_type=self._device_type,
            country=country,
            locale=normalized_locale,
        )
        self._source_scope = selected.source_scope
        self._locale_used = selected.locale_used or normalized_locale

        entry = selected.entry if isinstance(selected.entry, dict) else None
        self._catalog_generated_at = (
            str(catalog.get("generated_at"))
            if isinstance(catalog, dict) and catalog.get("generated_at") is not None
            else None
        )

        if entry is None:
            self._raw_latest_version = None
            self._attr_latest_version = None
            self._attr_release_url = None
            self._attr_release_summary = None
            _reconcile_skipped_version(self)
            return

        raw_latest = _text(entry.get("version"))
        normalized_latest = normalize_version_token(raw_latest)
        self._raw_latest_version = raw_latest
        self._attr_latest_version = _latest_version_for_state(
            latest=normalized_latest,
            installed=normalized_installed,
        )
        release_metadata_matches = _release_metadata_matches_state(
            catalog_version=normalized_latest,
            latest_version=self._attr_latest_version,
        )

        urls = entry.get("urls_by_locale")
        release_url = None
        if release_metadata_matches and isinstance(urls, dict):
            chosen_key = (
                self._locale_used
                if self._locale_used in urls
                else (str(next(iter(urls.keys()))) if urls else None)
            )
            if chosen_key is not None:
                release_url = _text(urls.get(chosen_key))
                self._locale_used = chosen_key

        self._attr_release_url = release_url
        self._attr_release_summary = (
            _text(entry.get("summary")) if release_metadata_matches else None
        )
        _reconcile_skipped_version(self)

    def _update_installed_version_history(self, version: str | None) -> None:
        if self._version_history is None:
            return
        self._installed_version_history = (
            self._version_history.record_installed_version(
                unique_id=self.unique_id,
                version=version,
                hass=self.hass if self.entity_id is not None else None,
                entity_id=self.entity_id,
                name=self.entity_id,
            )
        )


class ChargerFirmwareUpdateEntity(CoordinatorEntity[EnphaseCoordinator], UpdateEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_translation_key = "charger_firmware"

    def __init__(
        self,
        *,
        coordinator: EnphaseCoordinator,
        manager: EvseFirmwareDetailsManager,
        catalog_manager: FirmwareCatalogManager,
        serial: str,
        description: UpdateEntityDescription,
        version_history: FirmwareVersionHistoryStore | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._coord = coordinator
        self._manager = manager
        self._catalog_manager = catalog_manager
        self._serial = str(serial)
        self._version_history = version_history
        self._refresh_task = None

        self._attr_unique_id = _charger_update_unique_id(self._serial)
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

        self._raw_installed_version: str | None = None
        self._raw_latest_version: str | None = None
        self._upgrade_status: int | None = None
        self._status_detail: str | None = None
        self._last_successful_upgrade_date: str | None = None
        self._last_updated_at: str | None = None
        self._is_auto_ota: bool | None = None
        self._firmware_rollout_enabled: bool | None = None
        self._country_used: str | None = None
        self._locale_used: str = "en"
        self._source_scope: str | None = None
        self._catalog_generated_at: str | None = None
        self._catalog_latest_version: str | None = None
        self._installed_version_history: list[dict[str, str | None]] = []

        self._refresh_from_details(self._manager.cached_details)
        self._refresh_from_catalog(self._catalog_manager.cached_catalog)

    @property
    def available(self) -> bool:
        return (
            super().available
            and _type_available(self._coord, "iqevse")
            and self._serial in _charger_serials(self._coord)
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._serial)})

    @property
    def state(self) -> str | None:
        state = super().state
        if state != STATE_ON:
            return state  # type: ignore[no-any-return]
        if self._firmware_rollout_enabled is False:
            return STATE_OFF  # type: ignore[no-any-return]
        return state  # type: ignore[no-any-return]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._manager.status_snapshot()
        catalog_status = self._catalog_manager.status_snapshot()
        return {
            "upgrade_status": self._upgrade_status,
            "status_detail": self._status_detail,
            "last_successful_upgrade_date": self._last_successful_upgrade_date,
            "last_updated_at": self._last_updated_at,
            "is_auto_ota": self._is_auto_ota,
            "firmware_rollout_enabled": self._firmware_rollout_enabled,
            "catalog_source_scope": self._source_scope,
            "catalog_generated_at": self._catalog_generated_at,
            "catalog_last_error": catalog_status.get("last_error"),
            "details_last_error": status.get("last_error"),
            "details_using_stale": status.get("using_stale"),
            "installed_version_history": self._installed_version_history,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_refresh_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = None
        await super().async_will_remove_from_hass()

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Reject install requests; these entities only advertise firmware status."""
        raise HomeAssistantError(
            "Firmware updates are advisory only",
            translation_domain=DOMAIN,
            translation_key="firmware_advisory_only",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_from_details(self._manager.cached_details)
        self._refresh_from_catalog(self._catalog_manager.cached_catalog)
        self._schedule_details_refresh()
        super()._handle_coordinator_update()

    def _schedule_details_refresh(self) -> None:
        if self.hass is None:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = self.hass.async_create_task(
            self._async_refresh_state(),
            name=f"{DOMAIN}_evse_firmware_refresh_{redact_identifier(self._serial)}",
        )

    async def _async_refresh_details(self) -> None:
        try:
            details = await self._manager.async_get_details()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "EVSE firmware details refresh failed for %s: %s",
                redact_identifier(self._serial),
                redact_text(
                    err,
                    site_ids=(self._coord.site_id,),
                    identifiers=(self._serial,),
                ),
            )
            return

        self._refresh_from_details(details)

    async def _async_refresh_state(self) -> None:
        await self._async_refresh_details()
        await self._async_refresh_catalog()
        if self.hass is not None and self.entity_id is not None:
            self.async_write_ha_state()

    async def _async_refresh_catalog(self) -> None:
        try:
            catalog = await self._catalog_manager.async_get_catalog()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Firmware catalog refresh failed for charger %s: %s",
                redact_identifier(self._serial),
                redact_text(
                    err,
                    site_ids=(self._coord.site_id,),
                    identifiers=(self._serial,),
                ),
            )
            return

        self._refresh_from_catalog(catalog)

    def _refresh_from_details(
        self, details_by_serial: dict[str, dict[str, Any]] | None
    ) -> None:
        details = (
            details_by_serial.get(self._serial)
            if isinstance(details_by_serial, dict)
            else None
        )

        raw_installed = _text(details.get("currentFwVersion")) if details else None
        if raw_installed is None:
            raw_installed = _charger_installed_version(self._coord, self._serial)
        normalized_installed = normalize_version_token(raw_installed)
        self._raw_installed_version = raw_installed
        self._attr_installed_version = normalized_installed
        self._update_installed_version_history(normalized_installed)

        raw_latest = _text(details.get("targetFwVersion")) if details else None
        normalized_latest = normalize_version_token(raw_latest)
        self._raw_latest_version = raw_latest
        self._attr_latest_version = _latest_version_for_state(
            latest=normalized_latest,
            installed=normalized_installed,
        )
        self._clear_release_metadata_if_mismatch()

        self._upgrade_status = (
            _as_int(details.get("upgradeStatus")) if details else None
        )
        self._status_detail = _text(details.get("statusDetail")) if details else None
        self._last_successful_upgrade_date = (
            _text(details.get("lastSuccessfulUpgradeDate")) if details else None
        )
        self._last_updated_at = _text(details.get("lastUpdatedAt")) if details else None
        self._is_auto_ota = _as_bool(details.get("isAutoOta")) if details else None
        self._firmware_rollout_enabled = _evse_firmware_rollout_enabled(
            self._coord, self._serial
        )
        _reconcile_skipped_version(self)

    def _update_installed_version_history(self, version: str | None) -> None:
        if self._version_history is None:
            return
        self._installed_version_history = (
            self._version_history.record_installed_version(
                unique_id=self.unique_id,
                version=version,
                hass=self.hass if self.entity_id is not None else None,
                entity_id=self.entity_id,
                name=self.entity_id,
            )
        )

    def _refresh_from_catalog(self, catalog: dict[str, Any] | None) -> None:
        country, locale = resolve_country_and_locale(self._coord, self.hass)
        normalized_locale = normalize_locale(locale)

        self._country_used = country
        self._locale_used = normalized_locale

        selected = select_catalog_entry(
            catalog,
            device_type="iqevse",
            country=country,
            locale=normalized_locale,
        )
        self._source_scope = selected.source_scope
        self._locale_used = selected.locale_used or normalized_locale
        self._catalog_generated_at = (
            str(catalog.get("generated_at"))
            if isinstance(catalog, dict) and catalog.get("generated_at") is not None
            else None
        )
        self._catalog_latest_version = None

        entry = selected.entry if isinstance(selected.entry, dict) else None
        if entry is None:
            self._attr_release_url = None
            self._attr_release_summary = None
            return
        self._catalog_latest_version = normalize_version_token(
            _text(entry.get("version"))
        )

        urls = entry.get("urls_by_locale")
        release_url = None
        if self._release_metadata_matches_state() and isinstance(urls, dict):
            chosen_key = (
                self._locale_used
                if self._locale_used in urls
                else (str(next(iter(urls.keys()))) if urls else None)
            )
            if chosen_key is not None:
                release_url = _text(urls.get(chosen_key))
                self._locale_used = chosen_key

        self._attr_release_url = release_url
        self._attr_release_summary = (
            _text(entry.get("summary"))
            if self._release_metadata_matches_state()
            else None
        )

    def _release_metadata_matches_state(self) -> bool:
        return _release_metadata_matches_state(
            catalog_version=self._catalog_latest_version,
            latest_version=self.latest_version,
        )

    def _clear_release_metadata_if_mismatch(self) -> None:
        if self._release_metadata_matches_state():
            return
        self._attr_release_url = None
        self._attr_release_summary = None


def _charger_serials(coord: EnphaseCoordinator) -> list[str]:
    iter_serials = getattr(coord, "iter_serials", None)
    if callable(iter_serials):
        return [str(sn) for sn in iter_serials() if sn]
    return []


def _charger_update_unique_id(serial: str) -> str:
    return f"{DOMAIN}_{serial}_charger_firmware"


def _async_prune_removed_charger_updates(
    *,
    entry: EnphaseConfigEntry,
    ent_reg: Any,
    current_serials: set[str],
    known_serials: set[str],
    version_history: FirmwareVersionHistoryStore | None = None,
) -> None:
    unique_suffix = "_charger_firmware"
    unique_prefix = f"{DOMAIN}_"
    for reg_entry in list(ent_reg.entities.values()):
        entry_domain = getattr(reg_entry, "domain", None)
        if entry_domain is None:
            entry_domain = reg_entry.entity_id.partition(".")[0]
        if entry_domain != "update":
            continue
        entry_platform = getattr(reg_entry, "platform", None)
        if entry_platform is not None and entry_platform != DOMAIN:
            continue
        entry_config_id = getattr(reg_entry, "config_entry_id", None)
        if entry_config_id is not None and entry_config_id != entry.entry_id:
            continue
        unique_id = reg_entry.unique_id or ""
        if not (
            unique_id.startswith(unique_prefix) and unique_id.endswith(unique_suffix)
        ):
            continue
        serial = unique_id[len(unique_prefix) : -len(unique_suffix)]
        if not serial or serial in current_serials:
            continue
        ent_reg.async_remove(reg_entry.entity_id)
        if version_history is not None:
            version_history.remove(unique_id)
        known_serials.discard(serial)


def _gateway_installed_version(coord: EnphaseCoordinator) -> str | None:
    return _text(coord.inventory_view.type_device_sw_version("envoy"))


def _status_value(
    status: dict[str, Any] | None,
    key: str,
    default: Any = None,
) -> Any:
    if not isinstance(status, dict):
        return default
    return status.get(key, default)


def _charger_installed_version(coord: EnphaseCoordinator, serial: str) -> str | None:
    data = getattr(coord, "data", None)
    if isinstance(data, dict):
        payload = data.get(serial)
        if isinstance(payload, dict):
            for key in (
                "firmware_version",
                "system_version",
                "application_version",
                "sw_version",
            ):
                version = _text(payload.get(key))
                if version:
                    return cast(str | None, version)

    return _text(coord.inventory_view.type_device_sw_version("iqevse"))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _text(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except Exception:  # noqa: BLE001
        return None


def _evse_firmware_rollout_enabled(
    coord: EnphaseCoordinator, serial: str
) -> bool | None:
    feature_flag_enabled = getattr(coord, "evse_feature_flag_enabled", None)
    if not callable(feature_flag_enabled):
        return None
    try:
        return feature_flag_enabled("iqevse_itk_fw_upgrade_status", serial)  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001
        return None


def _latest_version_for_state(
    *, latest: str | None, installed: str | None
) -> str | None:
    comparable_update = compare_versions(latest, installed)
    if comparable_update is None:
        # Conservative fallback: avoid false-positive update state.
        return None
    if comparable_update:
        return latest
    return installed


def _release_metadata_matches_state(
    *, catalog_version: str | None, latest_version: str | None
) -> bool:
    return catalog_version is not None and catalog_version == latest_version


def _reconcile_skipped_version(entity: UpdateEntity) -> None:
    """Force Home Assistant to clear stale skipped firmware versions immediately."""
    entity.state_attributes
