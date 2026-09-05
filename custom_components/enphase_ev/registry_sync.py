"""Reconcile live registry topology and metadata."""

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
    _compose_charger_model_display,
    _is_redundant_model_id,
    _normalize_evse_display_name,
    _normalize_evse_model_name as _normalize_evse_model_name,
)
from .device_registry_compat import (
    device_belongs_to_config_entry,
    get_device_by_identifier,
    via_device_kwargs,
)
from .device_types import (
    is_dry_contact_type_key,
    normalize_type_key,
)
from .entity_cleanup import (
    entries_for_device,
    is_owned_entity,
    iter_device_registry_entries,
    iter_entity_registry_entries,
)
from .log_redaction import redact_identifier, redact_site_id, redact_text
from .runtime_data import EnphaseConfigEntry
from .runtime_helpers import coerce_optional_text as _clean_optional_text
from .serial_discovery import (
    active_serial_registry_identifiers,
)
from .serial_entity_metadata import (
    AC_BATTERY_ENTITY_UNIQUE_SUFFIXES,
    AC_BATTERY_RETIRED_UNIQUE_SUFFIXES,
    BATTERY_ENTITY_UNIQUE_SUFFIXES,
    BATTERY_RETIRED_UNIQUE_SUFFIXES,
    CHARGER_BINARY_SENSOR_UNIQUE_SUFFIXES,
    CHARGER_SENSOR_UNIQUE_SUFFIXES,
    ac_battery_entity_serial_from_unique_id,
    battery_entity_serial_from_unique_id,
    charger_entity_serial_from_unique_id,
    inverter_entity_serial_from_unique_id,
)

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator

_LOGGER = logging.getLogger(__name__)
_CHARGER_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "binary_sensor": CHARGER_BINARY_SENSOR_UNIQUE_SUFFIXES,
    "sensor": CHARGER_SENSOR_UNIQUE_SUFFIXES,
}
_TYPE_DEVICE_KEYS_WITH_DIRECT_CHILD_DEVICES: tuple[str, ...] = ("iqevse",)


def _sync_type_devices(
    entry: EnphaseConfigEntry,
    coord: EnphaseCoordinator,
    dev_reg: dr.DeviceRegistry,
    site_id: object,
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
        existing = get_device_by_identifier(dev_reg, ident, entry.entry_id)
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
    coord: EnphaseCoordinator,
    dev_reg: dr.DeviceRegistry,
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
        }
        kwargs.update(
            via_device_kwargs(dev_reg, via_device_id=None, legacy_via_device=None)
        )
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
        existing = get_device_by_identifier(dev_reg, (DOMAIN, sn), entry.entry_id)
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


def _serial_entity_group_from_unique_id(
    unique_id: object,
    *,
    domain: str,
    site_id: object,
) -> tuple[str, str] | None:
    """Return the serial-backed entity group and serial for a unique ID."""

    try:
        site_text = str(site_id).strip()
    except Exception:  # noqa: BLE001
        site_text = ""
    if domain == "binary_sensor":
        serial = charger_entity_serial_from_unique_id(
            unique_id,
            _CHARGER_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN["binary_sensor"],
        )
        return ("charger", serial) if serial is not None else None
    if domain != "sensor":
        return None

    charger_serial = charger_entity_serial_from_unique_id(
        unique_id,
        _CHARGER_ENTITY_UNIQUE_ID_SUFFIXES_BY_DOMAIN["sensor"],
    )
    if charger_serial is not None:
        return ("charger", charger_serial)
    if site_text:
        battery_serial = battery_entity_serial_from_unique_id(
            unique_id,
            site_id=site_text,
            suffixes=(
                *BATTERY_ENTITY_UNIQUE_SUFFIXES,
                *BATTERY_RETIRED_UNIQUE_SUFFIXES,
            ),
        )
        if battery_serial is not None:
            return ("battery", battery_serial)

        ac_battery_serial = ac_battery_entity_serial_from_unique_id(
            unique_id,
            site_id=site_text,
            suffixes=(
                *AC_BATTERY_ENTITY_UNIQUE_SUFFIXES,
                *AC_BATTERY_RETIRED_UNIQUE_SUFFIXES,
            ),
        )
        if ac_battery_serial is not None:
            return ("ac_battery", ac_battery_serial)

    inverter_serial = inverter_entity_serial_from_unique_id(unique_id)
    if inverter_serial is not None:
        return ("inverter", inverter_serial)
    return None


def _prune_inactive_serial_entities(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    coord: EnphaseCoordinator,
    site_id: object,
) -> int:
    """Remove owned serial-backed entities no longer present in active discovery."""

    if er is None or not bool(getattr(coord, "_devices_inventory_ready", False)):
        return 0
    try:
        ent_reg = er.async_get(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Skipping inactive serial entity cleanup for site %s: %s",
            redact_site_id(site_id),
            redact_text(err, site_ids=(site_id,)),
        )
        return 0
    active_serials_by_group = active_serial_registry_identifiers(coord)
    entry_id = getattr(entry, "entry_id", None)
    removed = 0
    for reg_entry in iter_entity_registry_entries(ent_reg):
        entry_domain = getattr(reg_entry, "domain", None)
        if entry_domain is None:
            entity_id = getattr(reg_entry, "entity_id", "")
            entry_domain = (
                entity_id.partition(".")[0] if isinstance(entity_id, str) else ""
            )
        if not is_owned_entity(reg_entry, entry_id, entry_domain):
            continue
        group_and_serial = _serial_entity_group_from_unique_id(
            getattr(reg_entry, "unique_id", None),
            domain=entry_domain,
            site_id=site_id,
        )
        if group_and_serial is None:
            continue
        group, serial = group_and_serial
        active_serials = active_serials_by_group.get(group)
        if active_serials is None:
            continue
        if serial in active_serials:
            continue
        entity_id = getattr(reg_entry, "entity_id", None)
        if not entity_id:
            continue
        try:
            ent_reg.async_remove(entity_id)
            removed += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed removing inactive serial entity %s for site %s: %s",
                redact_identifier(entity_id),
                redact_site_id(site_id),
                redact_text(err, site_ids=(site_id,), identifiers=(serial, entity_id)),
            )
    if removed:
        _LOGGER.debug(
            "Removed %s inactive serial entities for site %s",
            removed,
            redact_site_id(site_id),
        )
    return removed


def _remove_empty_inactive_serial_devices(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    coord: EnphaseCoordinator,
    dev_reg: dr.DeviceRegistry,
    site_id: object,
) -> int:
    """Remove empty serial devices no longer present in active inventory."""

    if er is None or not bool(getattr(coord, "_devices_inventory_ready", False)):
        return 0
    try:
        ent_reg = er.async_get(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Skipping inactive serial device cleanup for site %s: %s",
            redact_site_id(site_id),
            redact_text(err, site_ids=(site_id,)),
        )
        return 0
    remove_device = getattr(dev_reg, "async_remove_device", None)
    if not callable(remove_device):
        return 0

    active_serials_by_group = active_serial_registry_identifiers(coord)
    if any(serials is None for serials in active_serials_by_group.values()):
        return 0
    active_serials: set[str] = set()
    for serials in active_serials_by_group.values():
        active_serials.update(serials or set())
    entry_id = getattr(entry, "entry_id", None)
    removed = 0
    for device in iter_device_registry_entries(dev_reg):
        if not device_belongs_to_config_entry(device, entry_id):
            continue
        identifiers = getattr(device, "identifiers", None)
        if not identifiers:
            continue
        inactive_serials: list[str] = []
        has_active_serial = False
        for ident_domain, ident_value in identifiers:
            if ident_domain != DOMAIN:
                continue
            try:
                ident_text = str(ident_value).strip()
            except Exception:  # noqa: BLE001
                continue
            if (
                not ident_text
                or ident_text.startswith("type:")
                or ident_text.startswith("site:")
            ):
                continue
            if ident_text in active_serials:
                has_active_serial = True
                break
            inactive_serials.append(ident_text)
        if has_active_serial or not inactive_serials:
            continue
        device_id = getattr(device, "id", None)
        if not device_id:
            continue
        remaining_entries = entries_for_device(ent_reg, device_id)
        if remaining_entries:
            _LOGGER.debug(
                "Keeping inactive serial device %s for site %s; %s entities remain",
                redact_identifier(inactive_serials[0]),
                redact_site_id(site_id),
                len(remaining_entries),
            )
            continue
        try:
            remove_device(device_id)
            removed += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed removing inactive serial device %s for site %s: %s",
                redact_identifier(inactive_serials[0]),
                redact_site_id(site_id),
                redact_text(err, site_ids=(site_id,), identifiers=inactive_serials),
            )
    if removed:
        _LOGGER.debug(
            "Removed %s inactive serial devices for site %s",
            removed,
            redact_site_id(site_id),
        )
    return removed


def _sync_registry_devices(
    entry: EnphaseConfigEntry,
    coord: EnphaseCoordinator,
    dev_reg: dr.DeviceRegistry,
    site_id: object,
    *,
    hass: HomeAssistant | None = None,
    cleanup: bool = True,
) -> None:
    type_devices = _sync_type_devices(entry, coord, dev_reg, site_id)
    _sync_charger_devices(entry, coord, dev_reg, site_id, type_devices)
    if hass is not None and cleanup:
        _prune_inactive_serial_entities(hass, entry, coord, site_id)
        _remove_empty_inactive_serial_devices(hass, entry, coord, dev_reg, site_id)


def _registry_type_metadata_signature(
    coord: EnphaseCoordinator,
) -> tuple[tuple[object, ...], ...]:
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


def _registry_charger_metadata_signature(
    coord: EnphaseCoordinator,
) -> tuple[tuple[object, ...], ...]:
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


def _registry_metadata_signature(
    coord: EnphaseCoordinator,
) -> tuple[tuple[object, ...], ...]:
    return (
        ("types", *_registry_type_metadata_signature(coord)),
        ("chargers", *_registry_charger_metadata_signature(coord)),
    )
