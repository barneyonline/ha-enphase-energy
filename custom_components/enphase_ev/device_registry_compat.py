"""Compatibility helpers for Home Assistant device registry API transitions."""

from __future__ import annotations

from typing import cast

from homeassistant.helpers import device_registry as dr


def get_device_by_identifier(
    device_registry: dr.DeviceRegistry,
    identifier: tuple[str, str],
    config_entry_id: str,
) -> dr.DeviceEntry | None:
    """Return the device matching an identifier for one config entry."""

    scoped_lookup = getattr(device_registry, "async_get_device_by_identifier", None)
    if callable(scoped_lookup):
        return cast(dr.DeviceEntry | None, scoped_lookup(identifier, config_entry_id))
    return device_registry.async_get_device(identifiers={identifier})


def device_config_entry_ids(
    device: object,
    *,
    device_registry: dr.DeviceRegistry | None = None,
) -> tuple[str, ...]:
    """Return owning entry IDs, including legacy composite devices."""

    device_id = getattr(device, "id", None)
    get_composite_splits = (
        getattr(
            device_registry,
            "async_get_devices_for_composite_device_id",
            None,
        )
        if device_registry is not None
        else None
    )
    if device_id is not None and callable(get_composite_splits):
        split_devices = get_composite_splits(device_id)
        if split_devices:
            return tuple(
                dict.fromkeys(
                    str(split_device.config_entry_id) for split_device in split_devices
                )
            )

    config_entry_id = getattr(device, "config_entry_id", None)
    if config_entry_id is not None:
        return (str(config_entry_id),)

    # Home Assistant before 2026.8 only exposes config_entries. Composite
    # devices on newer versions are handled above through their real splits.
    return _legacy_config_entry_ids(device)


def _legacy_config_entry_ids(device: object) -> tuple[str, ...]:
    """Return plural owners from old HA or a synthesized composite device."""

    config_entries = getattr(device, "config_entries", None)
    if not config_entries:
        return ()
    return tuple(str(entry_id) for entry_id in config_entries)


def device_belongs_to_config_entry(device: object, config_entry_id: object) -> bool:
    """Return whether a device belongs to the specified config entry."""

    if config_entry_id is None:
        return False
    return str(config_entry_id) in device_config_entry_ids(device)


def via_device_kwargs(
    device_registry: dr.DeviceRegistry,
    *,
    via_device_id: str | None,
    legacy_via_device: tuple[str, str] | None,
) -> dict[str, object]:
    """Return the supported parent-device keyword for this HA version."""

    if callable(getattr(device_registry, "async_get_device_by_identifier", None)):
        return {"via_device_id": via_device_id}
    return {"via_device": legacy_via_device}
