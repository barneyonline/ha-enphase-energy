"""Normalize inventory response wrappers shared by onboarding and runtime."""

from __future__ import annotations


def hems_devices_groups(payload: object) -> list[dict[str, object]]:
    """Return grouped HEMS members from the dedicated HEMS inventory payload."""

    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, dict):
        devices = result.get("devices")
        if isinstance(devices, list):
            return [grouped for grouped in devices if isinstance(grouped, dict)]
        if isinstance(devices, dict):
            return [devices]
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    hems_devices = (
        data.get("hems-devices")
        if data.get("hems-devices") is not None
        else data.get("hems_devices")
    )
    if not isinstance(hems_devices, dict):
        return []
    return [hems_devices]
