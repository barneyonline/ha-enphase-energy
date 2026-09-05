"""Compatibility helpers for Home Assistant device registry API transitions."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr


def get_device_by_identifier(
    device_registry: dr.DeviceRegistry,
    identifier: tuple[str, str],
    config_entry_id: str,
) -> dr.DeviceEntry | None:
    """Return the device matching an identifier for one config entry."""

    return device_registry.async_get_device_by_identifier(identifier, config_entry_id)


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

    # Synthesized composite devices can still expose plural owners. Real
    # registry composites are handled above through their individual splits.
    return _legacy_config_entry_ids(device)


def _legacy_config_entry_ids(device: object) -> tuple[str, ...]:
    """Return plural owners from a synthesized composite device."""

    config_entries = getattr(device, "config_entries", None)
    if not config_entries:
        return ()
    return tuple(str(entry_id) for entry_id in config_entries)


def device_belongs_to_config_entry(device: object, config_entry_id: object) -> bool:
    """Return whether a device belongs to the specified config entry."""

    if config_entry_id is None:
        return False
    return str(config_entry_id) in device_config_entry_ids(device)
