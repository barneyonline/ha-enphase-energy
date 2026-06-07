"""Set up Enphase EV config entries, services, devices, and registry migrations."""

from __future__ import annotations

import asyncio
import logging
import re

from homeassistant.config_entries import ConfigEntryState, OperationNotAllowed
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)

from .const import CONF_INCLUDE_INVERTERS, CONF_SELECTED_TYPE_KEYS, DOMAIN
from .device_info_helpers import (
    _cloud_device_info,
    _compose_charger_model_display,
    _is_redundant_model_id,
    _normalize_evse_display_name,
    _normalize_evse_model_name as _normalize_evse_model_name,
    async_prime_integration_version,
)
from .device_types import (
    is_dry_contact_type_key,
    normalize_type_key,
    parse_type_identifier,
)
from .entity_cleanup import (
    entries_for_device,
    find_entity_id_by_unique_id,
    is_owned_entity,
    iter_device_registry_entries,
    iter_entity_registry_entries,
)
from .log_redaction import redact_identifier, redact_site_id, redact_text
from .runtime_data import EnphaseConfigEntry, EnphaseRuntimeData, get_runtime_data
from .runtime_helpers import coerce_optional_text as _clean_optional_text

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def async_setup_services(
    hass: HomeAssistant, *, supports_response: object = SupportsResponse
) -> None:
    """Register integration services without importing service schemas at module load."""

    from .services import async_setup_services as setup_services

    setup_services(hass, supports_response=supports_response)


def async_unload_services(hass: HomeAssistant) -> None:
    """Unload integration services without importing service schemas at module load."""

    from .services import async_unload_services as unload_services

    unload_services(hass)


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
]

_LEGACY_GATEWAY_TYPE_KEYS: tuple[str, ...] = ("meter", "enpower")
_SITE_ENERGY_ENTITY_UNIQUE_ID_SUFFIXES: tuple[str, ...] = (
    "solar_production",
    "consumption",
    "grid_import",
    "grid_export",
    "grid_power",
    "battery_charge",
    "battery_discharge",
    "battery_power",
)
_CLOUD_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "binary_sensor": ("cloud_reachable",),
    "sensor": (
        "last_update",
        "latency_ms",
        "current_production_power",
        "last_error_code",
        "backoff_ends",
        *_SITE_ENERGY_ENTITY_UNIQUE_ID_SUFFIXES,
    ),
}
_LEGACY_CLOUD_ENTITY_SUFFIX_ALIASES_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "sensor": (
        "current_power_consumption",
        "cloud_last_error",
        "cloud_last_error_code",
    ),
}
_STARTUP_MIGRATION_VERSION = 5
_STARTUP_MIGRATION_VERSION_KEY = "startup_migration_version"

_TYPE_DEVICE_KEYS_WITH_DIRECT_CHILD_DEVICES: tuple[str, ...] = ("iqevse",)

_entries_for_device = entries_for_device
_find_entity_id_by_unique_id = find_entity_id_by_unique_id
_is_owned_entity = is_owned_entity
_iter_device_registry_entries = iter_device_registry_entries
_iter_entity_registry_entries = iter_entity_registry_entries


def _site_entry_title(site_id: str) -> str:
    return f"Site: {site_id}"


def _startup_migration_version(entry: EnphaseConfigEntry) -> int:
    raw = entry.data.get(_STARTUP_MIGRATION_VERSION_KEY, 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_selected_type_keys(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        iterable = value
    elif isinstance(value, str):
        iterable = re.split(r"[,\n]+", value)
    else:
        iterable = []
    selected: list[str] = []
    for item in iterable:
        key = normalize_type_key(item)
        if key and key not in selected:
            selected.append(key)
    return selected


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


def _is_disabled_by_integration(disabled_by: object) -> bool:
    if disabled_by is None:
        return False
    value = getattr(disabled_by, "value", disabled_by)
    try:
        text = str(value).strip().lower()
    except Exception:  # noqa: BLE001
        return False
    return text == "integration"


async def _async_update_listener(
    hass: HomeAssistant, entry: EnphaseConfigEntry
) -> None:
    runtime_data = getattr(entry, "runtime_data", None)
    if isinstance(runtime_data, EnphaseRuntimeData):
        suppression_count = int(getattr(runtime_data, "reload_suppression_count", 0))
        if suppression_count > 0:
            # Coordinator-owned config-entry data updates persist tokens/cooldowns
            # and must not recreate the coordinator/client.
            runtime_data.reload_suppression_count = suppression_count - 1
            return
    if getattr(entry, "disabled_by", None) is not None:
        return
    loaded_state = getattr(ConfigEntryState, "LOADED", None)
    if loaded_state is not None and entry.state is not loaded_state:
        return
    try:
        await hass.config_entries.async_reload(entry.entry_id)
    except OperationNotAllowed as err:
        _LOGGER.debug(
            "Skipping reload for entry %s while state is changing: %s",
            entry.entry_id,
            err,
        )


async def _async_unload_platforms_safe(
    hass: HomeAssistant, entry: EnphaseConfigEntry
) -> bool:
    """Unload forwarded platforms, tolerating components that never loaded the entry."""

    async def _unload_platform(platform: str) -> bool:
        try:
            return await hass.config_entries.async_forward_entry_unload(entry, platform)
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


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Set up the integration domain and register services."""

    async_setup_services(hass, supports_response=SupportsResponse)
    return True


def _sync_type_devices(
    entry: EnphaseConfigEntry, coord, dev_reg, site_id: object
) -> dict[str, object]:
    """Create or update type devices from coordinator inventory."""

    inventory_view = coord.inventory_view
    type_devices: dict[str, object] = {}
    type_devices_by_identifier: dict[tuple[str, str], object] = {}
    type_keys = list(inventory_view.iter_type_keys())
    for type_key in type_keys:
        normalized = normalize_type_key(type_key)
        if is_dry_contact_type_key(type_key) or (
            normalized in _TYPE_DEVICE_KEYS_WITH_DIRECT_CHILD_DEVICES
        ):
            # Dry-contact and EV charger members get concrete child devices, so
            # adding another aggregate type device would duplicate the hierarchy.
            continue
        ident = inventory_view.type_identifier(type_key)
        if ident is None:
            continue
        if (
            isinstance(ident, tuple)
            and len(ident) == 2
            and ident in type_devices_by_identifier
        ):
            type_devices[type_key] = type_devices_by_identifier[ident]
            continue
        label = inventory_view.type_label(type_key)
        name = inventory_view.type_device_name(type_key)
        if not name:
            name = label
        model = inventory_view.type_device_model(type_key) or label
        hw_version = _clean_optional_text(
            inventory_view.type_device_hw_version(type_key)
        )
        serial_number = _clean_optional_text(
            inventory_view.type_device_serial_number(type_key)
        )
        model_id = _clean_optional_text(inventory_view.type_device_model_id(type_key))
        if _is_redundant_model_id(model, model_id):
            model_id = None
        sw_version = _inventory_type_device_sw_version_for_registry(
            inventory_view, type_key
        )
        if not label or not name:
            continue
        kwargs = {
            "config_entry_id": entry.entry_id,
            "identifiers": {ident},
            "manufacturer": "Enphase",
            "name": name,
            "model": model,
        }
        # Keep registry fields aligned with current coordinator data: clear stale
        # values by passing explicit None when helper methods return no value.
        kwargs["hw_version"] = hw_version
        kwargs["serial_number"] = serial_number
        kwargs["model_id"] = model_id
        kwargs["sw_version"] = sw_version
        existing = dev_reg.async_get_device(identifiers={ident})
        changes: list[str] = []
        if existing is None:
            changes.append("new_device")
        else:
            if existing.name != name:
                changes.append("name")
            if existing.manufacturer != "Enphase":
                changes.append("manufacturer")
            if existing.model != model:
                changes.append("model")
            if existing.hw_version != kwargs.get("hw_version"):
                changes.append("hw_version")
            if getattr(existing, "serial_number", None) != kwargs.get("serial_number"):
                changes.append("serial_number")
            if getattr(existing, "model_id", None) != kwargs.get("model_id"):
                changes.append("model_id")
            if getattr(existing, "sw_version", None) != kwargs.get("sw_version"):
                changes.append("sw_version")
        if changes:
            _LOGGER.debug(
                "Device registry update (%s) for type device %s (site=%s)",
                ",".join(changes),
                type_key,
                redact_site_id(site_id),
            )
        created = dev_reg.async_get_or_create(**kwargs)
        type_devices[type_key] = created
        if isinstance(ident, tuple) and len(ident) == 2:
            type_devices_by_identifier[ident] = created
    return type_devices


def _inventory_type_device_sw_version_for_registry(
    inventory_view: object, type_key: object
) -> str | None:
    sw_version_getter = getattr(inventory_view, "type_device_sw_version_summary", None)
    if not callable(sw_version_getter):
        sw_version_getter = getattr(inventory_view, "type_device_sw_version", None)
        if not callable(sw_version_getter):
            return None
        return _clean_optional_text(sw_version_getter(type_key))
    sw_version = _clean_optional_text(sw_version_getter(type_key))
    if sw_version is not None:
        return sw_version
    single_version_getter = getattr(inventory_view, "type_device_sw_version", None)
    if not callable(single_version_getter):
        return None
    return _clean_optional_text(single_version_getter(type_key))


def _sync_charger_devices(
    entry: EnphaseConfigEntry,
    coord,
    dev_reg,
    site_id: object,
    type_devices: dict[str, object],
) -> None:
    """Create or update charger devices and parent links."""
    iter_serials = getattr(coord, "iter_serials", None)
    serials = list(iter_serials()) if callable(iter_serials) else []
    data_source = coord.data if isinstance(getattr(coord, "data", None), dict) else {}
    for sn in serials:
        d = data_source.get(sn) or {}
        display_name = _normalize_evse_display_name(d.get("display_name"))
        fallback_name = _normalize_evse_display_name(d.get("name"))
        dev_name = display_name or fallback_name or f"Charger {sn}"
        kwargs = {
            "config_entry_id": entry.entry_id,
            "identifiers": {(DOMAIN, sn)},
            "manufacturer": "Enphase",
            "name": dev_name,
            "serial_number": str(sn),
            "via_device": None,
        }
        model_name_raw = d.get("model_name")
        model_display = _compose_charger_model_display(
            display_name,
            model_name_raw,
            dev_name,
        )
        if model_display:
            kwargs["model"] = model_display
        hw = d.get("hw_version")
        if hw:
            kwargs["hw_version"] = str(hw)
        sw = d.get("sw_version")
        if sw:
            kwargs["sw_version"] = str(sw)

        changes: list[str] = []
        existing = dev_reg.async_get_device(identifiers={(DOMAIN, sn)})
        if existing is None:
            changes.append("new_device")
        else:
            if existing.name != dev_name:
                changes.append("name")
            if existing.manufacturer != "Enphase":
                changes.append("manufacturer")
            if model_display and existing.model != model_display:
                changes.append("model")
            if hw and existing.hw_version != str(hw):
                changes.append("hw_version")
            if sw and existing.sw_version != str(sw):
                changes.append("sw_version")
            if existing.via_device_id is not None:
                changes.append("via_device")
        if changes:
            _LOGGER.debug(
                "Device registry update (%s) for charger serial=%s (site=%s)",
                ",".join(changes),
                redact_identifier(sn),
                redact_site_id(site_id),
            )
        dev_reg.async_get_or_create(**kwargs)


def _sync_registry_devices(
    entry: EnphaseConfigEntry, coord, dev_reg, site_id: object
) -> None:
    type_devices = _sync_type_devices(entry, coord, dev_reg, site_id)
    _sync_charger_devices(entry, coord, dev_reg, site_id, type_devices)


def _registry_type_metadata_signature(coord) -> tuple[tuple[object, ...], ...]:
    inventory_view = coord.inventory_view

    type_keys = list(inventory_view.iter_type_keys())
    signature: list[tuple[object, ...]] = []
    for type_key in type_keys:
        normalized = normalize_type_key(type_key)
        if is_dry_contact_type_key(type_key) or (
            normalized in _TYPE_DEVICE_KEYS_WITH_DIRECT_CHILD_DEVICES
        ):
            continue
        normalized = normalized or _clean_optional_text(type_key) or ""
        ident = inventory_view.type_identifier(type_key)
        signature.append(
            (
                normalized,
                ident,
                _clean_optional_text(inventory_view.type_label(type_key)),
                _clean_optional_text(inventory_view.type_device_name(type_key)),
                _clean_optional_text(inventory_view.type_device_model(type_key)),
                _clean_optional_text(inventory_view.type_device_hw_version(type_key)),
                _clean_optional_text(
                    inventory_view.type_device_serial_number(type_key)
                ),
                _clean_optional_text(inventory_view.type_device_model_id(type_key)),
                _inventory_type_device_sw_version_for_registry(
                    inventory_view, type_key
                ),
            )
        )
    return tuple(signature)


def _registry_charger_metadata_signature(coord) -> tuple[tuple[object, ...], ...]:
    iter_serials = getattr(coord, "iter_serials", None)
    serials = list(iter_serials()) if callable(iter_serials) else []
    data_source = coord.data if isinstance(getattr(coord, "data", None), dict) else {}
    signature: list[tuple[object, ...]] = []
    for sn in serials:
        payload = data_source.get(sn) or {}
        display_name = _normalize_evse_display_name(payload.get("display_name"))
        fallback_name = _normalize_evse_display_name(payload.get("name"))
        device_name = display_name or fallback_name or f"Charger {sn}"
        model_name_raw = payload.get("model_name")
        model_display = _compose_charger_model_display(
            display_name,
            model_name_raw,
            device_name,
        )
        signature.append(
            (
                str(sn),
                device_name,
                _clean_optional_text(model_display),
                _clean_optional_text(payload.get("model_id")),
                _clean_optional_text(payload.get("hw_version")),
                _clean_optional_text(payload.get("sw_version")),
            )
        )
    return tuple(signature)


def _registry_metadata_signature(coord) -> tuple[tuple[object, ...], ...]:
    return (
        ("types", *_registry_type_metadata_signature(coord)),
        ("chargers", *_registry_charger_metadata_signature(coord)),
    )


def _remove_legacy_inventory_entities(
    ent_reg, site_id: str, *, entry_id: str | None
) -> int:
    unique_ids = {
        f"{DOMAIN}_site_{site_id}_type_meter_inventory",
        f"{DOMAIN}_site_{site_id}_type_envoy_inventory",
        f"{DOMAIN}_site_{site_id}_type_microinverter_inventory",
    }
    removed = 0
    for entry in iter_entity_registry_entries(ent_reg):
        if not is_owned_entity(entry, entry_id):
            continue
        if getattr(entry, "unique_id", None) not in unique_ids:
            continue
        entity_id = getattr(entry, "entity_id", None)
        if not entity_id:
            continue
        try:
            ent_reg.async_remove(entity_id)
            removed += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed removing legacy inventory entity during migration for site %s: %s",
                redact_site_id(site_id),
                redact_text(err, site_ids=(site_id,)),
            )
    return removed


def _migrate_cloud_entity_unique_ids(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    site_id: object,
) -> None:
    """Migrate renamed cloud entity unique IDs without changing entity IDs."""

    if er is None:
        return
    try:
        site_id_text = str(site_id).strip()
    except Exception:  # noqa: BLE001
        site_id_text = ""
    if not site_id_text:
        return

    try:
        ent_reg = er.async_get(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Skipping cloud entity unique-id migration for site %s: %s",
            redact_site_id(site_id_text),
            redact_text(err, site_ids=(site_id_text,)),
        )
        return

    entry_id = getattr(entry, "entry_id", None)
    migrated = 0
    removed = 0
    # Keep entity IDs stable for users while moving old unique IDs to the
    # site-scoped naming convention used by current cloud sensors.
    rename_specs = (
        (
            "sensor",
            "current_production_power",
            (("current_power_consumption", False),),
        ),
        (
            "sensor",
            "last_error_code",
            (
                ("cloud_last_error_code", True),
                ("cloud_last_error", True),
            ),
        ),
    )

    def _candidate_unique_ids(
        suffix: str, *, include_legacy_prefix: bool
    ) -> tuple[str, ...]:
        unique_ids = [f"{DOMAIN}_site_{site_id_text}_{suffix}"]
        if include_legacy_prefix:
            unique_ids.append(f"{DOMAIN}_{site_id_text}_{suffix}")
        return tuple(unique_ids)

    for domain, new_suffix, source_specs in rename_specs:
        new_unique_id = f"{DOMAIN}_site_{site_id_text}_{new_suffix}"
        target_entity_id = find_entity_id_by_unique_id(
            ent_reg, domain, new_unique_id, entry_id=entry_id
        )
        source_entity_ids: list[tuple[str, str]] = []
        seen_entity_ids: set[str] = set()
        for old_suffix, include_legacy_prefix in source_specs:
            for old_unique_id in _candidate_unique_ids(
                old_suffix,
                include_legacy_prefix=include_legacy_prefix,
            ):
                old_entity_id = find_entity_id_by_unique_id(
                    ent_reg, domain, old_unique_id, entry_id=entry_id
                )
                if not old_entity_id or old_entity_id in seen_entity_ids:
                    continue
                source_entity_ids.append((old_suffix, old_entity_id))
                seen_entity_ids.add(old_entity_id)

        if not source_entity_ids:
            continue
        preserve_suffix, preserve_entity_id = source_entity_ids[0]

        if target_entity_id and target_entity_id != preserve_entity_id:
            try:
                ent_reg.async_remove(target_entity_id)
                removed += 1
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed removing duplicate migrated %s entity for site %s: %s",
                    new_suffix,
                    redact_site_id(site_id_text),
                    redact_text(err, site_ids=(site_id_text,)),
                )
                continue

        try:
            ent_reg.async_update_entity(preserve_entity_id, new_unique_id=new_unique_id)
            migrated += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed migrating %s unique_id to %s for site %s: %s",
                preserve_suffix,
                new_suffix,
                redact_site_id(site_id_text),
                redact_text(err, site_ids=(site_id_text,)),
            )
            continue

        for stale_suffix, stale_entity_id in source_entity_ids[1:]:
            try:
                ent_reg.async_remove(stale_entity_id)
                removed += 1
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed removing duplicate %s alias for site %s: %s",
                    stale_suffix,
                    redact_site_id(site_id_text),
                    redact_text(err, site_ids=(site_id_text,)),
                )

    if migrated:
        _LOGGER.debug(
            "Migrated %s cloud entity unique IDs for site %s",
            migrated,
            redact_site_id(site_id_text),
        )
    if removed:
        _LOGGER.debug(
            "Removed %s duplicate migrated cloud entities for site %s",
            removed,
            redact_site_id(site_id_text),
        )


def _migrate_cloud_entities_to_cloud_device(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    coord,
    dev_reg,
    site_id: object,
) -> None:
    if er is None:
        return
    site_id_raw = site_id
    if site_id_raw is None:
        site_id_raw = getattr(coord, "site_id", None)
    try:
        site_id_text = str(site_id_raw).strip()
    except Exception:  # noqa: BLE001
        site_id_text = ""
    if not site_id_text:
        return

    try:
        ent_reg = er.async_get(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Skipping cloud-device migration for site %s: %s",
            redact_site_id(site_id_text),
            redact_text(err, site_ids=(site_id_text,)),
        )
        return

    create_device = getattr(dev_reg, "async_get_or_create", None)
    if not callable(create_device):
        return
    cloud_info = _cloud_device_info(site_id_text)
    cloud_model = cloud_info.get("model")
    if not isinstance(cloud_model, str) or not cloud_model.strip():
        cloud_model = "Cloud Service"
    cloud_sw_version = cloud_info.get("sw_version")
    if not isinstance(cloud_sw_version, str) or not cloud_sw_version.strip():
        cloud_sw_version = None
    cloud_device = create_device(
        config_entry_id=getattr(entry, "entry_id", None),
        identifiers={(DOMAIN, f"type:{site_id_text}:cloud")},
        manufacturer="Enphase",
        name="Enphase Cloud",
        model=cloud_model,
        sw_version=cloud_sw_version,
        entry_type=getattr(getattr(dr, "DeviceEntryType", None), "SERVICE", None),
    )
    cloud_device_id = getattr(cloud_device, "id", None)
    if cloud_device_id is None:
        return

    entry_id = getattr(entry, "entry_id", None)
    moved = 0
    enabled = 0
    processed_entity_ids: set[str] = set()

    def _match_cloud_suffix(unique_id: str, candidates: tuple[str, ...]) -> str | None:
        for suffix in candidates:
            if unique_id.endswith(f"_{suffix}"):
                return suffix
        return None

    def _move_entity_to_cloud_device(entity_id: str, *, should_enable: bool) -> None:
        nonlocal moved, enabled
        if not entity_id or entity_id in processed_entity_ids:
            return
        processed_entity_ids.add(entity_id)
        get_entry = getattr(ent_reg, "async_get", None)
        reg_entry = get_entry(entity_id) if callable(get_entry) else None
        update_kwargs: dict[str, object] = {}
        if (
            reg_entry is None
            or getattr(reg_entry, "device_id", None) != cloud_device_id
        ):
            update_kwargs["device_id"] = cloud_device_id
        if should_enable and _is_disabled_by_integration(
            getattr(reg_entry, "disabled_by", None)
        ):
            update_kwargs["disabled_by"] = None
        if not update_kwargs:
            return
        try:
            ent_reg.async_update_entity(entity_id, **update_kwargs)
            if "device_id" in update_kwargs:
                moved += 1
            if "disabled_by" in update_kwargs:
                enabled += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed updating cloud entity %s for site %s: %s",
                entity_id,
                redact_site_id(site_id_text),
                redact_text(err, site_ids=(site_id_text,)),
            )

    all_cloud_suffixes_by_domain: dict[str, tuple[str, ...]] = {}
    for domain, suffixes in _CLOUD_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN.items():
        aliases = _LEGACY_CLOUD_ENTITY_SUFFIX_ALIASES_BY_DOMAIN.get(domain, ())
        combined = tuple(dict.fromkeys((*suffixes, *aliases)))
        all_cloud_suffixes_by_domain[domain] = combined

    for domain, unique_suffixes in _CLOUD_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN.items():
        for suffix in unique_suffixes:
            unique_id = f"{DOMAIN}_site_{site_id_text}_{suffix}"
            entity_id = find_entity_id_by_unique_id(
                ent_reg, domain, unique_id, entry_id=entry_id
            )
            if not entity_id:
                continue
            should_enable = bool(suffix in _SITE_ENERGY_ENTITY_UNIQUE_ID_SUFFIXES)
            _move_entity_to_cloud_device(entity_id, should_enable=should_enable)

    # Older releases used different unique_id prefixes for some cloud diagnostics.
    # Sweep owned entities and match by known cloud suffixes to catch those variants.
    site_marker = f"_site_{site_id_text}_"
    for reg_entry in iter_entity_registry_entries(ent_reg):
        if not is_owned_entity(reg_entry, entry_id):
            continue
        entity_id = getattr(reg_entry, "entity_id", None)
        if not entity_id:
            continue
        domain = getattr(reg_entry, "domain", None)
        if domain is None and isinstance(entity_id, str):
            domain = entity_id.partition(".")[0]
        if domain not in all_cloud_suffixes_by_domain:
            continue
        unique_id = getattr(reg_entry, "unique_id", None)
        if not isinstance(unique_id, str) or not unique_id:
            continue
        if "_site_" in unique_id and site_marker not in unique_id:
            continue
        suffix = _match_cloud_suffix(unique_id, all_cloud_suffixes_by_domain[domain])
        if suffix is None:
            continue
        should_enable = suffix in _SITE_ENERGY_ENTITY_UNIQUE_ID_SUFFIXES
        _move_entity_to_cloud_device(entity_id, should_enable=should_enable)

    if moved:
        _LOGGER.debug(
            "Migrated %s cloud entities to cloud device for site %s",
            moved,
            redact_site_id(site_id_text),
        )
    if enabled:
        _LOGGER.debug(
            "Enabled %s site energy entities by default for site %s",
            enabled,
            redact_site_id(site_id_text),
        )


def _migrate_legacy_gateway_type_devices(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    coord,
    dev_reg,
    site_id: object,
) -> None:
    if er is None:
        return
    site_id_raw = site_id
    if site_id_raw is None:
        site_id_raw = getattr(coord, "site_id", None)
    try:
        site_id_text = str(site_id_raw).strip()
    except Exception:  # noqa: BLE001
        site_id_text = ""
    if not site_id_text:
        return

    gateway_ident = coord.inventory_view.type_identifier("envoy") or (
        DOMAIN,
        f"type:{site_id_text}:envoy",
    )
    gateway_device = dev_reg.async_get_device(identifiers={gateway_ident})
    if gateway_device is None:
        return
    gateway_device_id = getattr(gateway_device, "id", None)
    if gateway_device_id is None:
        return

    try:
        ent_reg = er.async_get(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Skipping legacy type-device migration for site %s: %s",
            redact_site_id(site_id_text),
            redact_text(err, site_ids=(site_id_text,)),
        )
        return

    entry_id = getattr(entry, "entry_id", None)
    removed_inventory = _remove_legacy_inventory_entities(
        ent_reg, site_id_text, entry_id=entry_id
    )
    if removed_inventory:
        _LOGGER.debug(
            "Removed %s legacy inventory entities for site %s",
            removed_inventory,
            redact_site_id(site_id_text),
        )

    remove_device = getattr(dev_reg, "async_remove_device", None)

    def _move_device_to_gateway(legacy_device: object, type_key: str) -> None:
        legacy_device_id = getattr(legacy_device, "id", None)
        if legacy_device_id is None or legacy_device_id == gateway_device_id:
            return

        moved = 0
        for reg_entry in entries_for_device(ent_reg, legacy_device_id):
            if not is_owned_entity(reg_entry, entry_id):
                continue
            entity_id = getattr(reg_entry, "entity_id", None)
            if not entity_id:
                continue
            try:
                ent_reg.async_update_entity(entity_id, device_id=gateway_device_id)
                moved += 1
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed moving owned entity from legacy %s device to gateway for site %s: %s",
                    type_key,
                    redact_site_id(site_id_text),
                    redact_text(err, site_ids=(site_id_text,)),
                )

        remaining = entries_for_device(ent_reg, legacy_device_id)
        if remaining:
            _LOGGER.debug(
                "Keeping legacy %s type device for site %s; %s entities remain",
                type_key,
                redact_site_id(site_id_text),
                len(remaining),
            )
            return

        if callable(remove_device):
            try:
                remove_device(legacy_device_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed removing legacy %s type device for site %s: %s",
                    type_key,
                    redact_site_id(site_id_text),
                    redact_text(err, site_ids=(site_id_text,)),
                )
        if moved:
            _LOGGER.debug(
                "Migrated %s entities from legacy %s type device to gateway for site %s",
                moved,
                type_key,
                redact_site_id(site_id_text),
            )

    for type_key in _LEGACY_GATEWAY_TYPE_KEYS:
        legacy_ident = (DOMAIN, f"type:{site_id_text}:{type_key}")
        legacy_device = dev_reg.async_get_device(identifiers={legacy_ident})
        if legacy_device is None:
            continue
        _move_device_to_gateway(legacy_device, type_key)

    for legacy_device in iter_device_registry_entries(dev_reg):
        config_entries = getattr(legacy_device, "config_entries", None)
        if config_entries is not None and entry_id not in config_entries:
            continue
        identifiers = getattr(legacy_device, "identifiers", None)
        if not identifiers:
            continue
        matched_type_key: str | None = None
        for ident_domain, ident_value in identifiers:
            if ident_domain != DOMAIN:
                continue
            parsed = parse_type_identifier(ident_value)
            if parsed is None:
                continue
            ident_site_id, type_key = parsed
            if ident_site_id != site_id_text or not is_dry_contact_type_key(type_key):
                continue
            matched_type_key = type_key
            break
        if matched_type_key is None:
            continue
        _move_device_to_gateway(legacy_device, matched_type_key)

    legacy_site_ident = (DOMAIN, f"site:{site_id_text}")
    legacy_site_device = dev_reg.async_get_device(identifiers={legacy_site_ident})
    if legacy_site_device is None:
        return
    legacy_site_device_id = getattr(legacy_site_device, "id", None)
    if legacy_site_device_id is None or legacy_site_device_id == gateway_device_id:
        return

    moved_site_entities = 0
    for reg_entry in entries_for_device(ent_reg, legacy_site_device_id):
        if not is_owned_entity(reg_entry, entry_id):
            continue
        entity_id = getattr(reg_entry, "entity_id", None)
        if not entity_id:
            continue
        try:
            ent_reg.async_update_entity(entity_id, device_id=gateway_device_id)
            moved_site_entities += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed moving owned entity from legacy site device to gateway for site %s: %s",
                redact_site_id(site_id_text),
                redact_text(err, site_ids=(site_id_text,)),
            )

    remaining_site_entries = entries_for_device(ent_reg, legacy_site_device_id)
    if remaining_site_entries:
        _LOGGER.debug(
            "Keeping legacy site device for site %s; %s entities remain",
            redact_site_id(site_id_text),
            len(remaining_site_entries),
        )
        return

    if callable(remove_device):
        try:
            remove_device(legacy_site_device_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed removing legacy site device for site %s: %s",
                redact_site_id(site_id_text),
                redact_text(err, site_ids=(site_id_text,)),
            )
    if moved_site_entities:
        _LOGGER.debug(
            "Migrated %s entities from legacy site device to gateway for site %s",
            moved_site_entities,
            redact_site_id(site_id_text),
        )


def _migrate_orphaned_update_entities_to_type_devices(
    hass: HomeAssistant, entry: EnphaseConfigEntry, site_id: object
) -> None:
    if er is None:
        return
    try:
        site_id_text = str(site_id).strip()
    except Exception:  # noqa: BLE001
        site_id_text = ""
    if not site_id_text:
        return

    try:
        ent_reg = er.async_get(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Skipping orphaned update-entity migration for site %s: %s",
            redact_site_id(site_id_text),
            redact_text(err, site_ids=(site_id_text,)),
        )
        return

    entry_id = getattr(entry, "entry_id", None)
    gateway_unique_id = f"{DOMAIN}_site_{site_id_text}_envoy_firmware"
    microinverter_unique_id = f"{DOMAIN}_site_{site_id_text}_microinverter_firmware"
    removed_gateway_orphans = 0
    removed_microinverter_updates = 0

    for reg_entry in iter_entity_registry_entries(ent_reg):
        if not is_owned_entity(reg_entry, entry_id, "update"):
            continue
        unique_id = getattr(reg_entry, "unique_id", None)
        if unique_id == gateway_unique_id:
            if getattr(reg_entry, "device_id", None) is not None:
                continue
            entity_id = getattr(reg_entry, "entity_id", None)
            if not entity_id:
                continue
            ent_reg.async_remove(entity_id)
            removed_gateway_orphans += 1
            continue
        if unique_id != microinverter_unique_id:
            continue
        entity_id = getattr(reg_entry, "entity_id", None)
        if not entity_id:
            continue
        ent_reg.async_remove(entity_id)
        removed_microinverter_updates += 1

    if removed_gateway_orphans or removed_microinverter_updates:
        _LOGGER.debug(
            "Removed %s orphaned gateway firmware entities and %s deprecated microinverter firmware entities for site %s",
            removed_gateway_orphans,
            removed_microinverter_updates,
            redact_site_id(site_id_text),
        )


def _remove_evse_type_device_and_entities(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    dev_reg,
    site_id: object,
) -> None:
    if er is None:
        return
    try:
        site_id_text = str(site_id or entry.data.get("site_id", "")).strip()
    except Exception:  # noqa: BLE001
        site_id_text = ""
    if not site_id_text:
        return

    evse_ident = (DOMAIN, f"type:{site_id_text}:iqevse")
    evse_device = dev_reg.async_get_device(identifiers={evse_ident})
    if evse_device is None:
        return
    evse_device_id = getattr(evse_device, "id", None)
    if evse_device_id is None:
        return

    try:
        ent_reg = er.async_get(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Skipping EV charger type-device cleanup for site %s: %s",
            redact_site_id(site_id_text),
            redact_text(err, site_ids=(site_id_text,)),
        )
        return

    entry_id = getattr(entry, "entry_id", None)
    removed_entities = 0
    for reg_entry in entries_for_device(ent_reg, evse_device_id):
        if not is_owned_entity(reg_entry, entry_id):
            continue
        entity_id = getattr(reg_entry, "entity_id", None)
        if not entity_id:
            continue
        try:
            ent_reg.async_remove(entity_id)
            removed_entities += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed removing EV charger type entity %s for site %s: %s",
                entity_id,
                redact_site_id(site_id_text),
                redact_text(err, site_ids=(site_id_text,)),
            )

    remaining_entries = entries_for_device(ent_reg, evse_device_id)
    if remaining_entries:
        _LOGGER.debug(
            "Keeping EV charger type device for site %s; %s entities remain",
            redact_site_id(site_id_text),
            len(remaining_entries),
        )
        return

    remove_device = getattr(dev_reg, "async_remove_device", None)
    if callable(remove_device):
        try:
            remove_device(evse_device_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed removing EV charger type device for site %s: %s",
                redact_site_id(site_id_text),
                redact_text(err, site_ids=(site_id_text,)),
            )
            return
    if removed_entities:
        _LOGGER.debug(
            "Removed %s EV charger type entities and deleted type device for site %s",
            removed_entities,
            redact_site_id(site_id_text),
        )


def _complete_startup_migrations_if_ready(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    coord,
    dev_reg,
    site_id: object,
) -> None:
    if _startup_migration_version(entry) >= _STARTUP_MIGRATION_VERSION:
        return
    ready_check = getattr(coord, "startup_migrations_ready", None)
    if not callable(ready_check):
        return
    try:
        if not ready_check():
            return
    except Exception:  # noqa: BLE001
        return
    _migrate_cloud_entity_unique_ids(hass, entry, site_id)
    _migrate_legacy_gateway_type_devices(hass, entry, coord, dev_reg, site_id)
    _migrate_orphaned_update_entities_to_type_devices(hass, entry, site_id)
    _remove_evse_type_device_and_entities(hass, entry, dev_reg, site_id)
    _migrate_cloud_entities_to_cloud_device(hass, entry, coord, dev_reg, site_id)
    runtime_data = getattr(entry, "runtime_data", None)
    suppress_reload = isinstance(runtime_data, EnphaseRuntimeData)
    if isinstance(runtime_data, EnphaseRuntimeData):
        runtime_data.reload_suppression_count += 1
    migrated_data = dict(entry.data)
    migrated_data[_STARTUP_MIGRATION_VERSION_KEY] = _STARTUP_MIGRATION_VERSION
    try:
        changed = hass.config_entries.async_update_entry(entry, data=migrated_data)
    except Exception:
        if suppress_reload:
            runtime_data.reload_suppression_count = max(
                0,
                runtime_data.reload_suppression_count - 1,
            )
        raise
    if suppress_reload and not changed:
        runtime_data.reload_suppression_count = max(
            0,
            runtime_data.reload_suppression_count - 1,
        )


async def async_setup_entry(hass: HomeAssistant, entry: EnphaseConfigEntry) -> bool:
    migrated_data = _migrate_selected_type_keys(entry)
    if migrated_data is not None:
        hass.config_entries.async_update_entry(entry, data=migrated_data)

    site_id_text = str(entry.data.get("site_id", "")).strip()
    if site_id_text:
        desired_title = _site_entry_title(site_id_text)
        if entry.title != desired_title:
            hass.config_entries.async_update_entry(entry, title=desired_title)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    # Ensure services are present after config-entry reloads/transient unload states.
    async_setup_services(hass, supports_response=SupportsResponse)

    # Create and prime the coordinator once, used by all platforms
    from .coordinator import (
        EnphaseCoordinator,
    )  # local import to avoid heavy deps during non-HA imports
    from .battery_schedule_editor import BatteryScheduleEditorManager
    from .evse_schedule_editor import EvseScheduleEditorManager
    from .evse_firmware import EvseFirmwareDetailsManager
    from .firmware_catalog import FirmwareCatalogManager
    from .labels import async_prime_label_translations

    coord = EnphaseCoordinator(hass, entry.data, config_entry=entry)
    firmware_catalog = FirmwareCatalogManager(hass)
    evse_firmware_details = EvseFirmwareDetailsManager(lambda: coord.client)
    battery_schedule_editor = BatteryScheduleEditorManager(coord)
    evse_schedule_editor = EvseScheduleEditorManager(coord)
    setattr(coord, "firmware_catalog_manager", firmware_catalog)
    setattr(coord, "evse_firmware_details_manager", evse_firmware_details)
    entry.runtime_data = EnphaseRuntimeData(
        coordinator=coord,
        firmware_catalog=firmware_catalog,
        evse_firmware_details=evse_firmware_details,
        battery_schedule_editor=battery_schedule_editor,
        evse_schedule_editor=evse_schedule_editor,
    )
    discovery_snapshot = getattr(coord, "discovery_snapshot", None)
    restore_discovery_state = getattr(discovery_snapshot, "async_restore_state", None)
    if callable(restore_discovery_state):
        await restore_discovery_state()
    await async_prime_label_translations(hass)
    await coord.async_config_entry_first_refresh()
    battery_schedule_editor.sync_from_coordinator()
    evse_schedule_editor.sync_from_coordinator()
    await async_prime_integration_version(hass)

    site_id = entry.data.get("site_id")
    dev_reg = dr.async_get(hass)
    _sync_registry_devices(entry, coord, dev_reg, site_id)
    _remove_evse_type_device_and_entities(hass, entry, dev_reg, site_id)
    _migrate_orphaned_update_entities_to_type_devices(hass, entry, site_id)
    _complete_startup_migrations_if_ready(hass, entry, coord, dev_reg, site_id)
    last_registry_signature = _registry_metadata_signature(coord)

    def _sync_registry_on_update() -> None:
        nonlocal last_registry_signature

        try:
            current_signature = _registry_metadata_signature(coord)
            if current_signature != last_registry_signature:
                _sync_registry_devices(entry, coord, dev_reg, site_id)
                _remove_evse_type_device_and_entities(hass, entry, dev_reg, site_id)
                _migrate_orphaned_update_entities_to_type_devices(hass, entry, site_id)
                last_registry_signature = current_signature
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
        entry.async_on_unload(add_state_listener(_sync_registry_on_update))

    schedule_sync = getattr(coord, "schedule_sync", None)
    if schedule_sync is not None and hasattr(schedule_sync, "async_add_listener"):
        entry.async_on_unload(
            schedule_sync.async_add_listener(evse_schedule_editor.sync_from_coordinator)
        )

    def _schedule_background_task(coro, name: str) -> None:
        entry_create_background = getattr(entry, "async_create_background_task", None)
        hass_create_background = getattr(hass, "async_create_background_task", None)
        if callable(entry_create_background):
            entry_create_background(hass, coro, name)
        elif callable(hass_create_background):
            hass_create_background(coro, name)
        else:
            hass.async_create_task(coro, name=name)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start background work only after entities have been forwarded so restored
    # topology can create entities first and warmup can fill in live state later.
    schedule_sync = getattr(coord, "schedule_sync", None)
    if schedule_sync is not None and hasattr(schedule_sync, "async_start"):
        _schedule_background_task(
            schedule_sync.async_start(),
            f"{DOMAIN}_schedule_sync_start",
        )

    refresh_runner = getattr(coord, "refresh_runner", None)
    startup_warmup = getattr(refresh_runner, "async_start_startup_warmup", None)
    if callable(startup_warmup):
        _schedule_background_task(
            startup_warmup(),
            f"{DOMAIN}_startup_warmup",
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnphaseConfigEntry) -> bool:
    coord = None
    try:
        coord = get_runtime_data(entry).coordinator
    except RuntimeError:
        pass
    unload_ok = await _async_unload_platforms_safe(hass, entry)
    if unload_ok:
        if coord is not None and hasattr(coord, "schedule_sync"):
            await coord.schedule_sync.async_stop()
        if coord is not None and hasattr(coord, "cleanup_runtime_state"):
            coord.cleanup_runtime_state()
        entry.runtime_data = None
        loaded_state = getattr(ConfigEntryState, "LOADED", None)
        has_loaded_entries = any(
            loaded_state is not None and config_entry.state is loaded_state
            for config_entry in hass.config_entries.async_entries(DOMAIN)
        )
        if not has_loaded_entries:
            async_unload_services(hass)
    return unload_ok
