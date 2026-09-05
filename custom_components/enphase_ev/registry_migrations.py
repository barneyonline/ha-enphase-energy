"""Conservative one-time migrations of integration registry records."""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
)
from .const import (
    DOMAIN,
)
from .device_info_helpers import (
    _cloud_device_info,
    _normalize_evse_model_name as _normalize_evse_model_name,
)
from .device_registry_compat import (
    device_belongs_to_config_entry,
    get_device_by_identifier,
)
from .device_types import (
    is_dry_contact_type_key,
    parse_type_identifier,
)
from .entity_cleanup import (
    entries_for_device,
    find_entity_id_by_unique_id,
    is_owned_entity,
    iter_device_registry_entries,
    iter_entity_registry_entries,
    prune_managed_entities,
)
from .log_redaction import redact_site_id, redact_text
from .runtime_data import EnphaseConfigEntry, EnphaseRuntimeData

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)
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
_STARTUP_MIGRATION_VERSION = 6
_STARTUP_MIGRATION_VERSION_KEY = "startup_migration_version"


def _remove_legacy_inventory_entities(
    ent_reg: er.EntityRegistry, site_id: str, *, entry_id: str | None
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
    coord: EnphaseCoordinator,
    dev_reg: dr.DeviceRegistry,
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
        entry_domain = getattr(reg_entry, "domain", None)
        if entry_domain is None and isinstance(entity_id, str):
            entry_domain = entity_id.partition(".")[0]
        if entry_domain not in all_cloud_suffixes_by_domain:
            continue
        entry_unique_id = getattr(reg_entry, "unique_id", None)
        if not isinstance(entry_unique_id, str) or not entry_unique_id:
            continue
        if "_site_" in entry_unique_id and site_marker not in entry_unique_id:
            continue
        matched_suffix = _match_cloud_suffix(
            entry_unique_id, all_cloud_suffixes_by_domain[entry_domain]
        )
        if matched_suffix is None:
            continue
        should_enable = matched_suffix in _SITE_ENERGY_ENTITY_UNIQUE_ID_SUFFIXES
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
    coord: EnphaseCoordinator,
    dev_reg: dr.DeviceRegistry,
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
    gateway_device = get_device_by_identifier(dev_reg, gateway_ident, entry.entry_id)
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
        legacy_device = get_device_by_identifier(dev_reg, legacy_ident, entry.entry_id)
        if legacy_device is None:
            continue
        _move_device_to_gateway(legacy_device, type_key)

    for legacy_device in iter_device_registry_entries(dev_reg):
        if not device_belongs_to_config_entry(legacy_device, entry_id):
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

    _remove_legacy_site_device(hass, entry, coord, dev_reg, site_id_text)


def _is_legacy_site_device(device: object, legacy_site_ident: tuple[str, str]) -> bool:
    """Return True for the old site-only placeholder device."""

    identifiers = getattr(device, "identifiers", None)
    if not identifiers:
        return False
    return set(identifiers) == {legacy_site_ident}


def _remove_legacy_site_device(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    coord: EnphaseCoordinator,
    dev_reg: dr.DeviceRegistry,
    site_id: object,
) -> None:
    """Remove the legacy Enphase Site placeholder device."""

    if er is None:
        return
    try:
        site_id_text = str(site_id).strip()
    except Exception:  # noqa: BLE001
        site_id_text = ""
    if not site_id_text:
        return

    entry_id = getattr(entry, "entry_id", None)
    gateway_device_id = None
    gateway_ident = coord.inventory_view.type_identifier("envoy") or (
        DOMAIN,
        f"type:{site_id_text}:envoy",
    )
    gateway_device = get_device_by_identifier(dev_reg, gateway_ident, entry.entry_id)
    if gateway_device is not None:
        gateway_device_id = getattr(gateway_device, "id", None)

    legacy_site_ident = (DOMAIN, f"site:{site_id_text}")
    legacy_site_device = get_device_by_identifier(
        dev_reg, legacy_site_ident, entry.entry_id
    )
    if legacy_site_device is None:
        return
    if not _is_legacy_site_device(legacy_site_device, legacy_site_ident):
        return
    if not device_belongs_to_config_entry(legacy_site_device, entry_id):
        return
    legacy_site_device_id = getattr(legacy_site_device, "id", None)
    if legacy_site_device_id is None:
        return
    if gateway_device_id is not None and legacy_site_device_id == gateway_device_id:
        return

    try:
        ent_reg = er.async_get(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Skipping legacy site-device cleanup for site %s: %s",
            redact_site_id(site_id_text),
            redact_text(err, site_ids=(site_id_text,)),
        )
        return

    moved_site_entities = 0
    for reg_entry in entries_for_device(ent_reg, legacy_site_device_id):
        if not is_owned_entity(reg_entry, entry_id):
            continue
        if gateway_device_id is None:
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

    remove_device = getattr(dev_reg, "async_remove_device", None)
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
    dev_reg: dr.DeviceRegistry,
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
    evse_device = get_device_by_identifier(dev_reg, evse_ident, entry.entry_id)
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
    coord: EnphaseCoordinator,
    dev_reg: dr.DeviceRegistry,
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
    _remove_legacy_site_device(hass, entry, coord, dev_reg, site_id)
    _migrate_legacy_gateway_type_devices(hass, entry, coord, dev_reg, site_id)
    _migrate_orphaned_update_entities_to_type_devices(hass, entry, site_id)
    _remove_evse_type_device_and_entities(hass, entry, dev_reg, site_id)
    _migrate_cloud_entities_to_cloud_device(hass, entry, coord, dev_reg, site_id)
    _remove_retired_grid_profile_device_entities(hass, entry, site_id)
    runtime_data = getattr(entry, "runtime_data", None)
    typed_runtime_data = (
        runtime_data if isinstance(runtime_data, EnphaseRuntimeData) else None
    )
    migrated_data = dict(entry.data)
    migrated_data[_STARTUP_MIGRATION_VERSION_KEY] = _STARTUP_MIGRATION_VERSION
    if typed_runtime_data is not None:
        typed_runtime_data.mark_internal_data_update(dict(entry.data), migrated_data)
    try:
        changed = hass.config_entries.async_update_entry(entry, data=migrated_data)
    except Exception:
        if typed_runtime_data is not None:
            typed_runtime_data.unmark_internal_data_update()
        raise
    if typed_runtime_data is not None and not changed:
        typed_runtime_data.unmark_internal_data_update()


def _remove_retired_grid_profile_device_entities(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    site_id: object,
) -> None:
    """Remove grid-profile controls retired from the device page."""

    site_id_text = str(site_id or "").strip()
    if not site_id_text:
        return
    ent_reg = er.async_get(hass)
    sensor_current_unique_id = f"{DOMAIN}_site_{site_id_text}_current_grid_profile"
    current_entity_id = find_entity_id_by_unique_id(
        ent_reg,
        "sensor",
        sensor_current_unique_id,
        entry_id=entry.entry_id,
    )
    if current_entity_id:
        try:
            ent_reg.async_update_entity(current_entity_id, entity_category=None)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed clearing Grid Profile entity category for site %s: %s",
                redact_site_id(site_id_text),
                redact_text(err, site_ids=(site_id_text,)),
            )
    prune_managed_entities(
        ent_reg,
        entry.entry_id,
        domain="sensor",
        active_unique_ids={sensor_current_unique_id},
        is_managed=lambda unique_id: unique_id
        in {
            f"{DOMAIN}_site_{site_id_text}_grid_profile_status",
            sensor_current_unique_id,
            f"{DOMAIN}_site_{site_id_text}_requested_grid_profile",
        },
    )
    prune_managed_entities(
        ent_reg,
        entry.entry_id,
        domain="button",
        active_unique_ids=set(),
        is_managed=lambda unique_id: unique_id
        == f"{DOMAIN}_site_{site_id_text}_apply_staged_grid_profile",
    )
    prune_managed_entities(
        ent_reg,
        entry.entry_id,
        domain="select",
        active_unique_ids=set(),
        is_managed=lambda unique_id: unique_id
        in {
            f"{DOMAIN}_site_{site_id_text}_grid_profile_region",
            f"{DOMAIN}_site_{site_id_text}_grid_profile_list_mode",
            f"{DOMAIN}_site_{site_id_text}_grid_profile_staged_profile",
        },
    )


def _is_disabled_by_integration(disabled_by: object) -> bool:
    if disabled_by is None:
        return False
    value = getattr(disabled_by, "value", disabled_by)
    try:
        text = str(value).strip().lower()
    except Exception:  # noqa: BLE001
        return False
    return text == "integration"


def _startup_migration_version(entry: EnphaseConfigEntry) -> int:
    raw = entry.data.get(_STARTUP_MIGRATION_VERSION_KEY, 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0
