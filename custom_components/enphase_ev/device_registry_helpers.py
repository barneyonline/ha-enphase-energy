"""Device ownership helpers, including migrated composite devices."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr


def device_config_entry_ids(
    device: object,
    *,
    device_registry: dr.DeviceRegistry | None = None,
) -> tuple[str, ...]:
    """Return owning entry IDs, including legacy composite devices."""

    device_id = getattr(device, "id", None)
    if device_id is not None and device_registry is not None:
        split_devices = device_registry.async_get_devices_for_composite_device_id(
            device_id
        )
        if split_devices:
            return tuple(
                dict.fromkeys(
                    str(split_device.config_entry_id) for split_device in split_devices
                )
            )

    config_entry_id = getattr(device, "config_entry_id", None)
    if config_entry_id is not None:
        return (str(config_entry_id),)

    return ()


def device_belongs_to_config_entry(device: object, config_entry_id: object) -> bool:
    """Return whether a device belongs to the specified config entry."""

    if config_entry_id is None:
        return False
    return str(config_entry_id) in device_config_entry_ids(device)
