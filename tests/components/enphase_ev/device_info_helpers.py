"""Assertion helpers for Home Assistant device metadata."""

from collections.abc import Mapping
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo


def device_info_fields(
    info: DeviceInfo | Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Return the set fields, including fields explicitly set to None."""
    assert info is not None
    if (as_dict := getattr(info, "as_dict", None)) is not None:
        return as_dict()
    return info
