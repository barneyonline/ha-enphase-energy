from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .log_redaction import redact_identifier, redact_text

_LOGGER = logging.getLogger(__name__)


def iter_entity_registry_entries(ent_reg) -> list[object]:
    """Best-effort iteration over entity registry entries."""
    entities = getattr(ent_reg, "entities", None)
    if entities is None:
        return []
    values = getattr(entities, "values", None)
    if callable(values):
        try:
            return list(values())
        except Exception:  # noqa: BLE001
            return []
    if isinstance(entities, dict):
        return list(dict.values(entities))
    return []


def iter_device_registry_entries(dev_reg) -> list[object]:
    """Best-effort iteration over device registry entries."""
    devices = getattr(dev_reg, "devices", None)
    if devices is None:
        return []
    values = getattr(devices, "values", None)
    if callable(values):
        try:
            return list(values())
        except Exception:  # noqa: BLE001
            return []
    if isinstance(devices, dict):
        return list(dict.values(devices))
    return []


def entries_for_device(ent_reg, device_id: str) -> list[object]:
    """Return entity registry entries attached to the given device."""
    entries: list[object] = []
    seen_entity_ids: set[object] = set()
    entries_for_device_func = getattr(er, "async_entries_for_device", None)
    if callable(entries_for_device_func):
        try:
            for entry in entries_for_device_func(ent_reg, device_id):
                entries.append(entry)
                seen_entity_ids.add(getattr(entry, "entity_id", id(entry)))
        except Exception:  # noqa: BLE001
            pass
    for entry in iter_entity_registry_entries(ent_reg):
        if getattr(entry, "device_id", None) != device_id:
            continue
        entity_id = getattr(entry, "entity_id", id(entry))
        if entity_id in seen_entity_ids:
            continue
        entries.append(entry)
        seen_entity_ids.add(entity_id)
    return entries


def is_owned_entity(
    reg_entry: object, entry_id: str | None, domain: str | None = None
) -> bool:
    """Return True when the registry entry belongs to this integration entry."""
    entry_domain = getattr(reg_entry, "domain", None)
    if entry_domain is None:
        entity_id = getattr(reg_entry, "entity_id", "")
        entry_domain = entity_id.partition(".")[0] if isinstance(entity_id, str) else ""
    if domain is not None and entry_domain != domain:
        return False

    entry_platform = getattr(reg_entry, "platform", None)
    if entry_platform is not None and entry_platform != DOMAIN:
        return False

    config_entry_id = getattr(reg_entry, "config_entry_id", None)
    if (
        entry_id is not None
        and config_entry_id is not None
        and config_entry_id != entry_id
    ):
        return False
    return True


def find_entity_id_by_unique_id(
    ent_reg,
    domain: str,
    unique_id: str,
    *,
    entry_id: str | None,
) -> str | None:
    """Resolve a managed entity_id from a unique_id."""
    get_entity_id = getattr(ent_reg, "async_get_entity_id", None)
    get_entry = getattr(ent_reg, "async_get", None)
    if callable(get_entity_id):
        try:
            entity_id = get_entity_id(domain, DOMAIN, unique_id)
        except Exception:  # noqa: BLE001
            entity_id = None
        if entity_id:
            if callable(get_entry):
                reg_entry = get_entry(entity_id)
                if reg_entry is not None and not is_owned_entity(
                    reg_entry, entry_id, domain
                ):
                    return None
            return entity_id

    for reg_entry in iter_entity_registry_entries(ent_reg):
        if getattr(reg_entry, "unique_id", None) != unique_id:
            continue
        entry_domain = getattr(reg_entry, "domain", None)
        if entry_domain is None:
            entity_id = getattr(reg_entry, "entity_id", "")
            entry_domain = (
                entity_id.partition(".")[0] if isinstance(entity_id, str) else None
            )
        if entry_domain != domain:
            continue
        if not is_owned_entity(reg_entry, entry_id, domain):
            continue
        entity_id = getattr(reg_entry, "entity_id", None)
        if entity_id:
            return entity_id
    return None


def prune_managed_entities(
    ent_reg,
    entry_id: str | None,
    *,
    domain: str,
    active_unique_ids: Iterable[str],
    is_managed: Callable[[str], bool],
) -> int:
    """Remove managed owned entities that are no longer active."""
    active = set(active_unique_ids)
    removed = 0
    for reg_entry in list(iter_entity_registry_entries(ent_reg)):
        if not is_owned_entity(reg_entry, entry_id, domain):
            continue
        unique_id = getattr(reg_entry, "unique_id", None)
        if not isinstance(unique_id, str) or not is_managed(unique_id):
            continue
        if unique_id in active:
            continue
        entity_id = getattr(reg_entry, "entity_id", None)
        if not entity_id:
            continue
        try:
            ent_reg.async_remove(entity_id)
            removed += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed removing stale %s entity %s: %s",
                domain,
                redact_identifier(entity_id),
                redact_text(err),
            )
    return removed
