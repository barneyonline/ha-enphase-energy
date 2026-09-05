"""Set up Enphase EV config entries, services, devices, and registry migrations."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import time as _time
from typing import TYPE_CHECKING, Any, Coroutine, cast

from homeassistant.config_entries import ConfigEntryState, OperationNotAllowed
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)

from .const import (
    CONF_EMAIL,
    CONF_INCLUDE_INVERTERS,
    CONF_PASSWORD,
    CONF_REMEMBER_PASSWORD,
    CONF_SELECTED_TYPE_KEYS,
    CONF_SERIALS,
    CONF_SITE_NAME,
    CONF_SITE_ONLY,
    DOMAIN,
    DEFAULT_GRID_PROFILE_CONTROLS_ENABLED,
    OPT_GRID_PROFILE_CONTROLS_ENABLED,
    OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
    OPT_MICROINVERTER_POWER_ENABLED,
    OPT_WEATHER_ENABLED,
    OPT_VPP_EVENTS_ENABLED,
)
from .device_info_helpers import (
    _cloud_device_info as _cloud_device_info,
    _compose_charger_model_display as _compose_charger_model_display,
    _normalize_evse_model_name as _normalize_evse_model_name,
    async_prime_integration_version,
)
from .entity_cleanup import (
    entries_for_device,
    find_entity_id_by_unique_id,
    is_owned_entity,
    iter_device_registry_entries,
    iter_entity_registry_entries,
)
from .log_redaction import redact_site_id, redact_text
from .runtime_data import EnphaseConfigEntry, EnphaseRuntimeData, get_runtime_data
from .reload_snapshot import ReloadSnapshot
from .config_selection import (
    normalize_selected_type_keys as _normalize_selected_type_keys,
)

from .registry_sync import (
    _sync_type_devices as _sync_type_devices,
    _inventory_type_device_sw_version_for_registry as _inventory_type_device_sw_version_for_registry,
    _sync_charger_devices as _sync_charger_devices,
    _serial_entity_group_from_unique_id as _serial_entity_group_from_unique_id,
    _prune_inactive_serial_entities,
    _remove_empty_inactive_serial_devices,
    _sync_registry_devices,
    _registry_type_metadata_signature as _registry_type_metadata_signature,
    _registry_charger_metadata_signature as _registry_charger_metadata_signature,
    _registry_metadata_signature,
)
from .registry_migrations import (
    _remove_legacy_inventory_entities as _remove_legacy_inventory_entities,
    _migrate_cloud_entity_unique_ids as _migrate_cloud_entity_unique_ids,
    _migrate_cloud_entities_to_cloud_device as _migrate_cloud_entities_to_cloud_device,
    _migrate_legacy_gateway_type_devices as _migrate_legacy_gateway_type_devices,
    _is_legacy_site_device as _is_legacy_site_device,
    _remove_legacy_site_device as _remove_legacy_site_device,
    _migrate_orphaned_update_entities_to_type_devices as _migrate_orphaned_update_entities_to_type_devices,
    _remove_evse_type_device_and_entities as _remove_evse_type_device_and_entities,
    _complete_startup_migrations_if_ready,
    _remove_retired_grid_profile_device_entities as _remove_retired_grid_profile_device_entities,
    _is_disabled_by_integration as _is_disabled_by_integration,
    _startup_migration_version as _startup_migration_version,
)

from .registry_sync import (
    _CHARGER_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN as _CHARGER_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN,
)
from .registry_sync import (
    _TYPE_DEVICE_KEYS_WITH_DIRECT_CHILD_DEVICES as _TYPE_DEVICE_KEYS_WITH_DIRECT_CHILD_DEVICES,
)
from .registry_migrations import _LEGACY_GATEWAY_TYPE_KEYS as _LEGACY_GATEWAY_TYPE_KEYS
from .registry_migrations import (
    _SITE_ENERGY_ENTITY_UNIQUE_ID_SUFFIXES as _SITE_ENERGY_ENTITY_UNIQUE_ID_SUFFIXES,
)
from .registry_migrations import (
    _CLOUD_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN as _CLOUD_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN,
)
from .registry_migrations import (
    _LEGACY_CLOUD_ENTITY_SUFFIX_ALIASES_BY_DOMAIN as _LEGACY_CLOUD_ENTITY_SUFFIX_ALIASES_BY_DOMAIN,
)
from .registry_migrations import (
    _STARTUP_MIGRATION_VERSION as _STARTUP_MIGRATION_VERSION,
)
from .registry_migrations import (
    _STARTUP_MIGRATION_VERSION_KEY as _STARTUP_MIGRATION_VERSION_KEY,
)

if TYPE_CHECKING:  # pragma: no cover
    import aiohttp

    from .coordinator import EnphaseCoordinator as EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)

_RUNTIME_HANDOFF_KEY = f"{DOMAIN}_runtime_handoffs"
_RELOAD_REQUIRED_OPTION_KEYS = frozenset(
    {
        OPT_GRID_PROFILE_CONTROLS_ENABLED,
        OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
        OPT_MICROINVERTER_POWER_ENABLED,
        OPT_WEATHER_ENABLED,
        OPT_VPP_EVENTS_ENABLED,
    }
)
_HOT_APPLY_DATA_KEYS = frozenset({CONF_EMAIL, CONF_PASSWORD, CONF_REMEMBER_PASSWORD})
_RUNTIME_HANDOFF_DATA_KEYS = frozenset(
    {
        CONF_INCLUDE_INVERTERS,
        CONF_SELECTED_TYPE_KEYS,
        CONF_SERIALS,
        CONF_SITE_NAME,
        CONF_SITE_ONLY,
    }
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_SETUP_MODULES = (
    "aiohttp",
    f"{__package__}.battery_schedule_editor",
    f"{__package__}.coordinator",
    f"{__package__}.evse_firmware",
    f"{__package__}.evse_schedule_editor",
    f"{__package__}.firmware_catalog",
    f"{__package__}.gateway_software_update",
    f"{__package__}.labels",
)


def _load_setup_modules() -> None:
    """Load setup-only modules from Home Assistant's executor."""

    for module_name in _SETUP_MODULES:
        importlib.import_module(module_name)


def async_create_clientsession(*args: Any, **kwargs: Any) -> aiohttp.ClientSession:
    """Create a Home Assistant client session without loading it at import time."""

    from homeassistant.helpers.aiohttp_client import (
        async_create_clientsession as create_clientsession,
    )

    return create_clientsession(*args, **kwargs)


def async_setup_services(
    hass: HomeAssistant, *, supports_response: type[Any] = SupportsResponse
) -> None:
    """Register integration services without importing service schemas at module load."""

    from .services import async_setup_services as setup_services

    setup_services(hass, supports_response=supports_response)


PLATFORMS: list[str] = [
    "sensor",
    "binary_sensor",
    "button",
    "select",
    "number",
    "switch",
    "time",
    "calendar",
    "update",
    "weather",
]

_LEGACY_GRID_TOGGLE_OPTION = "grid_toggle_enabled"


_entries_for_device = entries_for_device
_find_entity_id_by_unique_id = find_entity_id_by_unique_id
_is_owned_entity = is_owned_entity
_iter_device_registry_entries = iter_device_registry_entries
_iter_entity_registry_entries = iter_entity_registry_entries


def _site_entry_title(site_id: str) -> str:
    return f"Site: {site_id}"


def _migrate_selected_type_keys(entry: EnphaseConfigEntry) -> dict[str, object] | None:
    if CONF_SELECTED_TYPE_KEYS not in entry.data:
        return None
    raw_selected = entry.data.get(CONF_SELECTED_TYPE_KEYS, [])
    normalized_selected = _normalize_selected_type_keys(raw_selected)
    include_inverters = bool(entry.data.get(CONF_INCLUDE_INVERTERS, True))
    if include_inverters and "microinverter" not in normalized_selected:
        normalized_selected.append("microinverter")
    if not include_inverters:
        normalized_selected = [
            key for key in normalized_selected if key != "microinverter"
        ]
    if raw_selected == normalized_selected:
        return None
    updated = dict(entry.data)
    updated[CONF_SELECTED_TYPE_KEYS] = normalized_selected
    return updated


def _remove_retired_grid_control_entities(
    hass: HomeAssistant, entry: EnphaseConfigEntry
) -> None:
    """Remove Grid Mode control entities retired in config-entry minor version 2."""

    site_id = str(entry.data.get("site_id", "") or "").strip()
    if not site_id:
        return
    ent_reg = er.async_get(hass)
    for domain, suffix in (
        ("button", "request_grid_toggle_otp"),
        ("sensor", "grid_control_status"),
    ):
        entity_id = find_entity_id_by_unique_id(
            ent_reg,
            domain,
            f"{DOMAIN}_site_{site_id}_{suffix}",
            entry_id=entry.entry_id,
        )
        if entity_id is not None:
            ent_reg.async_remove(entity_id)


async def async_migrate_entry(hass: HomeAssistant, entry: EnphaseConfigEntry) -> bool:
    """Migrate Enphase config entries to the latest schema."""

    if entry.version != 1:
        return False
    if entry.minor_version < 2:
        options = dict(entry.options)
        options.pop(_LEGACY_GRID_TOGGLE_OPTION, None)
        _remove_retired_grid_control_entities(hass, entry)
        hass.config_entries.async_update_entry(
            entry,
            options=options,
            version=1,
            minor_version=2,
        )
    if entry.minor_version < 3:
        options = dict(entry.options)
        if OPT_GRID_PROFILE_CONTROLS_ENABLED not in options:
            site_id = str(entry.data.get("site_id", "") or "").strip()
            ent_reg = er.async_get(hass)
            current_profile_entity_id = None
            if site_id:
                current_profile_entity_id = find_entity_id_by_unique_id(
                    ent_reg,
                    "sensor",
                    f"{DOMAIN}_site_{site_id}_current_grid_profile",
                    entry_id=entry.entry_id,
                )
            options[OPT_GRID_PROFILE_CONTROLS_ENABLED] = bool(current_profile_entity_id)
        hass.config_entries.async_update_entry(
            entry,
            options=options,
            version=1,
            minor_version=3,
        )
    return True


async def _async_update_listener_locked(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    runtime_data: EnphaseRuntimeData | None,
) -> None:
    target_data = dict(entry.data)
    target_options = dict(entry.options)
    if isinstance(runtime_data, EnphaseRuntimeData):
        # HA schedules update callbacks after mutating the entry. A user update
        # may therefore be visible to an earlier internal persistence callback.
        runtime_data.consume_internal_data_updates()
    if getattr(entry, "disabled_by", None) is not None:
        return
    loaded_state = getattr(ConfigEntryState, "LOADED", None)
    if loaded_state is not None and entry.state is not loaded_state:
        return
    if isinstance(runtime_data, EnphaseRuntimeData):
        previous_data = runtime_data.applied_data
        previous_options = runtime_data.applied_options
        preserve_runtime = False
        if previous_data is not None and previous_options is not None:
            changed_data_keys = {
                key
                for key in previous_data.keys() | target_data.keys()
                if previous_data.get(key) != target_data.get(key)
            }
            changed_option_keys = {
                key
                for key in previous_options.keys() | target_options.keys()
                if previous_options.get(key) != target_options.get(key)
            }
            if not changed_data_keys and not changed_option_keys:
                return
            reload_required = bool(
                changed_data_keys - _HOT_APPLY_DATA_KEYS
                or changed_option_keys & _RELOAD_REQUIRED_OPTION_KEYS
            )
            preserve_runtime = not bool(
                changed_data_keys - _HOT_APPLY_DATA_KEYS - _RUNTIME_HANDOFF_DATA_KEYS
            )
            if not reload_required:
                coord = runtime_data.coordinator
                try:
                    coord.apply_auth_storage_config(target_data)
                    await coord.async_apply_config_entry_options(previous_options)
                except Exception:  # noqa: BLE001 - fall back to the proven reload path
                    _LOGGER.exception(
                        "Failed to apply Enphase options live; reloading entry %s",
                        entry.entry_id,
                    )
                else:
                    runtime_data.applied_data = target_data
                    runtime_data.applied_options = target_options
                    return

        runtime_data.preserve_for_reload = preserve_runtime
    try:
        reloaded = await hass.config_entries.async_reload(entry.entry_id)
        if reloaded is False and isinstance(runtime_data, EnphaseRuntimeData):
            runtime_data.preserve_for_reload = False
    except OperationNotAllowed as err:
        if isinstance(runtime_data, EnphaseRuntimeData):
            runtime_data.preserve_for_reload = False
        _LOGGER.debug(
            "Skipping reload for entry %s while state is changing: %s",
            entry.entry_id,
            err,
        )
    except BaseException:
        if isinstance(runtime_data, EnphaseRuntimeData):
            runtime_data.preserve_for_reload = False
        raise


async def _async_update_listener(
    hass: HomeAssistant, entry: EnphaseConfigEntry
) -> None:
    runtime_data = getattr(entry, "runtime_data", None)
    if not isinstance(runtime_data, EnphaseRuntimeData):
        await _async_update_listener_locked(hass, entry, None)
        return
    async with runtime_data.update_listener_lock:
        if getattr(entry, "runtime_data", None) is not runtime_data:
            return
        await _async_update_listener_locked(hass, entry, runtime_data)


async def _async_unload_platforms_safe(
    hass: HomeAssistant, entry: EnphaseConfigEntry
) -> bool:
    """Unload forwarded platforms, tolerating components that never loaded the entry."""

    async def _unload_platform(platform: str) -> bool:
        try:
            return cast(
                bool,
                await hass.config_entries.async_forward_entry_unload(entry, platform),
            )
        except ValueError as err:
            if str(err) != "Config entry was never loaded!":
                raise
            _LOGGER.debug(
                "Skipping unload for platform %s on entry %s because it never loaded",
                platform,
                entry.entry_id,
            )
            return True

    return all(
        await asyncio.gather(*(_unload_platform(platform) for platform in PLATFORMS))
    )


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Set up the integration domain and register services."""

    async_setup_services(hass, supports_response=SupportsResponse)
    return True


async def _async_setup_entry_impl(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    preserved_snapshot: ReloadSnapshot | None,
) -> bool:
    setup_started = _time.monotonic()
    setup_timings: dict[str, float] = {}

    def _record_phase(key: str, started: float) -> None:
        setup_timings[key] = round(max(0.0, _time.monotonic() - started), 3)

    migrated_data = _migrate_selected_type_keys(entry)
    if migrated_data is not None:
        hass.config_entries.async_update_entry(entry, data=migrated_data)

    site_id_text = str(entry.data.get("site_id", "")).strip()
    if site_id_text:
        desired_title = _site_entry_title(site_id_text)
        if entry.title != desired_title:
            hass.config_entries.async_update_entry(entry, title=desired_title)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.async_add_import_executor_job(_load_setup_modules)

    # Create and prime the coordinator once, used by all platforms
    from .coordinator import (
        EnphaseCoordinator as EnphaseCoordinator,
    )
    from .battery_schedule_editor import (
        BatteryScheduleEditorManager as BatteryScheduleEditorManager,
    )
    from .evse_schedule_editor import (
        EvseScheduleEditorManager as EvseScheduleEditorManager,
    )
    from .evse_firmware import EvseFirmwareDetailsManager as EvseFirmwareDetailsManager
    from .firmware_catalog import FirmwareCatalogManager as FirmwareCatalogManager
    from .gateway_software_update import (
        GatewaySoftwareUpdateManager as GatewaySoftwareUpdateManager,
    )
    from .labels import async_prime_label_translations as async_prime_label_translations

    coordinator_started = _time.monotonic()
    reused_runtime = isinstance(preserved_snapshot, ReloadSnapshot)
    import aiohttp

    coord = EnphaseCoordinator(
        hass,
        entry.data,
        config_entry=entry,
        cookie_header_session=async_create_clientsession(
            hass,
            auto_cleanup=True,
            cookie_jar=aiohttp.DummyCookieJar(),
        ),
    )
    entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    firmware_catalog = FirmwareCatalogManager(hass)
    evse_firmware_details = EvseFirmwareDetailsManager(lambda: coord.client)
    gateway_software_update = GatewaySoftwareUpdateManager(
        lambda: coord.client,
        lambda: coord.inventory_view.type_device_serial_number("envoy"),
    )
    battery_schedule_editor = BatteryScheduleEditorManager(coord)
    evse_schedule_editor = EvseScheduleEditorManager(coord)
    setattr(coord, "firmware_catalog_manager", firmware_catalog)
    setattr(coord, "evse_firmware_details_manager", evse_firmware_details)
    entry.runtime_data.firmware_catalog = firmware_catalog
    entry.runtime_data.evse_firmware_details = evse_firmware_details
    entry.runtime_data.gateway_software_update = gateway_software_update
    entry.runtime_data.battery_schedule_editor = battery_schedule_editor
    entry.runtime_data.evse_schedule_editor = evse_schedule_editor
    entry.runtime_data.applied_data = dict(entry.data)
    entry.runtime_data.applied_options = dict(entry.options)
    if preserved_snapshot is not None:
        coord.restore_reload_snapshot(preserved_snapshot)
    _record_phase("coordinator_init_s", coordinator_started)
    setup_milestones: dict[str, float] = {}
    begin_setup_tracking = getattr(coord, "begin_setup_tracking", None)
    if callable(begin_setup_tracking):
        begin_setup_tracking(setup_started, setup_timings, setup_milestones)
    else:
        # Compatibility for lightweight coordinators supplied by downstream tests.
        coord._setup_started_mono = setup_started
        coord._setup_phase_timings = setup_timings
        coord._setup_milestones = setup_milestones

    async def _prime_labels() -> None:
        started = _time.monotonic()
        try:
            await async_prime_label_translations(hass)
        finally:
            _record_phase("translations_s", started)

    async def _prime_version() -> None:
        started = _time.monotonic()
        try:
            await async_prime_integration_version(hass)
        finally:
            _record_phase("integration_version_s", started)

    label_task = asyncio.create_task(
        _prime_labels(), name=f"{DOMAIN}_startup_translations"
    )
    version_task = asyncio.create_task(
        _prime_version(), name=f"{DOMAIN}_startup_version"
    )
    try:
        bootstrap_first_refresh = getattr(coord, "async_bootstrap_first_refresh", None)
        if reused_runtime:
            await coord.async_bootstrap_cached_refresh()
            setup_timings["first_refresh_s"] = 0.0
        elif callable(bootstrap_first_refresh):
            await bootstrap_first_refresh()
        else:
            # Compatibility for lightweight coordinators supplied by downstream tests.
            discovery_snapshot = getattr(coord, "discovery_snapshot", None)
            restore_discovery_state = getattr(
                discovery_snapshot, "async_restore_state", None
            )
            snapshot_started = _time.monotonic()
            if callable(restore_discovery_state):
                await restore_discovery_state()
            _record_phase("snapshot_restore_s", snapshot_started)

            refresh_runner = getattr(coord, "refresh_runner", None)
            start_power = getattr(refresh_runner, "async_start_startup_power", None)
            if callable(start_power):
                await start_power()

            first_refresh_started = _time.monotonic()
            await coord.async_config_entry_first_refresh()
            _record_phase("first_refresh_s", first_refresh_started)
        mark_setup_milestone = getattr(coord, "mark_setup_milestone", None)
        if callable(mark_setup_milestone):
            mark_setup_milestone("core_ready")
        else:
            setup_milestones["core_ready"] = round(_time.monotonic() - setup_started, 3)
        await asyncio.gather(label_task, version_task)
    except BaseException:
        for task in (label_task, version_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(label_task, version_task, return_exceptions=True)
        cancel_startup_power = getattr(coord, "async_cancel_startup_power", None)
        if callable(cancel_startup_power):
            await cancel_startup_power()
        else:  # pragma: no cover - lightweight downstream coordinator compatibility
            startup_power_task = getattr(coord, "_startup_power_task", None)
            if (
                isinstance(startup_power_task, asyncio.Future)
                and not startup_power_task.done()
            ):
                startup_power_task.cancel()
                await asyncio.gather(startup_power_task, return_exceptions=True)
        raise

    editor_sync_started = _time.monotonic()
    battery_schedule_editor.sync_from_coordinator()
    evse_schedule_editor.sync_from_coordinator()
    _record_phase("editor_sync_s", editor_sync_started)

    site_id = entry.data.get("site_id")
    dev_reg = dr.async_get(hass)
    registry_started = _time.monotonic()
    _sync_registry_devices(entry, coord, dev_reg, site_id, hass=hass, cleanup=False)
    _complete_startup_migrations_if_ready(hass, entry, coord, dev_reg, site_id)
    last_registry_signature = _registry_metadata_signature(coord)
    _record_phase("registry_reconcile_s", registry_started)

    def _sync_registry_on_update() -> None:
        nonlocal last_registry_signature

        try:
            current_signature = _registry_metadata_signature(coord)
            if current_signature != last_registry_signature:
                _sync_registry_devices(entry, coord, dev_reg, site_id, hass=hass)
                last_registry_signature = current_signature
            else:
                _prune_inactive_serial_entities(hass, entry, coord, site_id)
                _remove_empty_inactive_serial_devices(
                    hass, entry, coord, dev_reg, site_id
                )
            _complete_startup_migrations_if_ready(hass, entry, coord, dev_reg, site_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Skipping registry sync for site %s after update: %s",
                redact_site_id(site_id),
                redact_text(err, site_ids=(site_id,)),
            )

    add_topology_listener = getattr(coord, "async_add_topology_listener", None)
    if callable(add_topology_listener):
        entry.async_on_unload(add_topology_listener(_sync_registry_on_update))

    add_state_listener = getattr(coord, "async_add_listener", None)
    if callable(add_state_listener):
        entry.async_on_unload(
            add_state_listener(battery_schedule_editor.sync_from_coordinator)
        )
        entry.async_on_unload(
            add_state_listener(evse_schedule_editor.sync_from_coordinator)
        )

    schedule_sync = getattr(coord, "schedule_sync", None)
    if schedule_sync is not None and hasattr(schedule_sync, "async_add_listener"):
        entry.async_on_unload(
            schedule_sync.async_add_listener(evse_schedule_editor.sync_from_coordinator)
        )

    def _schedule_background_task(coro: Coroutine[Any, Any, Any], name: str) -> None:
        entry_create_background = getattr(entry, "async_create_background_task", None)
        hass_create_background = getattr(hass, "async_create_background_task", None)
        if callable(hass_create_background):
            task = hass_create_background(coro, name)
        elif callable(entry_create_background):
            task = entry_create_background(hass, coro, name)
        else:
            task = hass.async_create_task(coro, name=name)
        track_background_task = getattr(coord, "track_entry_background_task", None)
        if callable(track_background_task):
            track_background_task(task)

    platform_started = _time.monotonic()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _record_phase("platform_forward_s", platform_started)
    mark_setup_milestone = getattr(coord, "mark_setup_milestone", None)
    if callable(mark_setup_milestone):
        mark_setup_milestone("entities_forwarded")
    else:
        setup_milestones["entities_forwarded"] = round(
            _time.monotonic() - setup_started, 3
        )
    # Start background work only after entities have been forwarded so restored
    # topology can create entities first and warmup can fill in live state later.
    # Schedule warmup first so the bounded startup power stage gets priority over
    # other optional background work competing for the shared read limiter.
    startup_warmup = getattr(coord, "async_start_startup_warmup", None)
    if not callable(startup_warmup):
        refresh_runner = getattr(coord, "refresh_runner", None)
        startup_warmup = getattr(refresh_runner, "async_start_startup_warmup", None)
    if callable(startup_warmup):
        _schedule_background_task(
            startup_warmup(),
            f"{DOMAIN}_startup_warmup",
        )

    if reused_runtime:
        _schedule_background_task(
            coord.async_request_refresh(),
            f"{DOMAIN}_reload_refresh",
        )

    grid_profile_startup_probe = getattr(
        coord, "async_refresh_grid_profile_metadata", None
    )
    grid_profile_controls_enabled = bool(
        entry.options.get(
            OPT_GRID_PROFILE_CONTROLS_ENABLED,
            DEFAULT_GRID_PROFILE_CONTROLS_ENABLED,
        )
    )
    if grid_profile_controls_enabled and callable(grid_profile_startup_probe):
        _schedule_background_task(
            grid_profile_startup_probe(force=True),
            f"{DOMAIN}_grid_profile_startup_probe",
        )

    schedule_sync = getattr(coord, "schedule_sync", None)
    if schedule_sync is not None and hasattr(schedule_sync, "async_start"):
        _schedule_background_task(
            schedule_sync.async_start(),
            f"{DOMAIN}_schedule_sync_start",
        )

    total_setup_seconds = _time.monotonic() - setup_started
    finish_setup_tracking = getattr(coord, "finish_setup_tracking", None)
    if callable(finish_setup_tracking):
        finish_setup_tracking(total_setup_seconds)
    else:
        setup_timings["total_s"] = round(total_setup_seconds, 3)
        coord._setup_phase_timings = dict(setup_timings)
        setup_milestones["setup_complete"] = setup_timings["total_s"]
    return True


async def _async_cleanup_failed_runtime(
    entry: EnphaseConfigEntry,
    runtime_data: EnphaseRuntimeData,
) -> None:
    """Release all runtime resources when config-entry setup does not complete."""

    coord = runtime_data.coordinator
    cleanup_steps = (
        runtime_data.async_stop_weather,
        getattr(getattr(coord, "schedule_sync", None), "async_stop", None),
        getattr(coord, "async_cleanup_runtime_state", None),
        getattr(coord, "async_close", None),
    )
    for cleanup in cleanup_steps:
        if not callable(cleanup):
            continue
        try:
            result = cleanup()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - continue releasing the remaining resources
            _LOGGER.exception(
                "Failed cleaning up runtime for entry %s",
                entry.entry_id,
            )


async def async_setup_entry(hass: HomeAssistant, entry: EnphaseConfigEntry) -> bool:
    """Set up an Enphase config entry, restoring detached cached state when safe."""

    handoffs = hass.data.get(_RUNTIME_HANDOFF_KEY)
    snapshot = (
        handoffs.pop(entry.entry_id, None) if isinstance(handoffs, dict) else None
    )
    try:
        return await _async_setup_entry_impl(
            hass, entry, snapshot if isinstance(snapshot, ReloadSnapshot) else None
        )
    except BaseException:
        runtime_data = getattr(entry, "runtime_data", None)
        if isinstance(runtime_data, EnphaseRuntimeData):
            await _async_cleanup_failed_runtime(entry, runtime_data)
            if getattr(entry, "runtime_data", None) is runtime_data:
                entry.runtime_data = None
        raise


async def async_unload_entry(hass: HomeAssistant, entry: EnphaseConfigEntry) -> bool:
    coord = None
    runtime_data = None
    try:
        runtime_data = get_runtime_data(entry)
        coord = runtime_data.coordinator
    except RuntimeError:
        pass
    unload_ok = await _async_unload_platforms_safe(hass, entry)
    if unload_ok:
        if runtime_data is not None:
            await runtime_data.async_stop_weather()
        if coord is not None and hasattr(coord, "schedule_sync"):
            await coord.schedule_sync.async_stop()
        preserve_runtime = bool(
            runtime_data is not None and runtime_data.preserve_for_reload
        )
        if preserve_runtime:
            async_quiesce_for_reload = getattr(coord, "async_quiesce_for_reload", None)
            if callable(async_quiesce_for_reload):
                preserve_runtime = await async_quiesce_for_reload() is not False
            if not preserve_runtime and runtime_data is not None:
                runtime_data.preserve_for_reload = False
        if preserve_runtime:
            capture = getattr(coord, "capture_reload_snapshot", None)
            if callable(capture):
                snapshot = capture()
                handoffs = hass.data.setdefault(_RUNTIME_HANDOFF_KEY, {})
                handoffs[entry.entry_id] = snapshot
        async_cleanup_runtime_state = getattr(
            coord, "async_cleanup_runtime_state", None
        )
        if callable(async_cleanup_runtime_state):
            await async_cleanup_runtime_state()
        elif coord is not None and hasattr(coord, "cleanup_runtime_state"):
            coord.cleanup_runtime_state()
        if coord is not None and hasattr(coord, "async_close"):
            await coord.async_close()
        entry.runtime_data = None
    return unload_ok
