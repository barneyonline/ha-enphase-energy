"""Pure normalization of stored and user-selected device categories."""

from __future__ import annotations

import re
from collections.abc import Collection

from .device_types import normalize_type_key


def normalize_serials(value: object) -> list[str]:
    """Keep serial selection ordering and the historical input vocabulary."""

    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = re.split(r"[,\n]+", value)
    else:
        items = []
    return list(dict.fromkeys(text for item in items if (text := str(item).strip())))


def normalize_selected_type_keys(
    value: object, *, allowed: Collection[str] | None = None
) -> list[str]:
    """Normalize aliases, optionally restricting selectable categories."""

    if isinstance(value, (list, tuple, set)):
        items = value
    elif isinstance(value, str):
        items = re.split(r"[,\n]+", value)
    else:
        items = []
    return list(
        dict.fromkeys(
            key
            for item in items
            if (key := normalize_type_key(item)) and (allowed is None or key in allowed)
        )
    )
