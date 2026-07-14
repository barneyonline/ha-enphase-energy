"""Helpers for immutable runtime snapshots."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


def freeze_snapshot_value(value: object) -> object:
    """Recursively detach mutable containers for a public snapshot."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: freeze_snapshot_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_snapshot_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_snapshot_value(item) for item in value)
    return value


def freeze_snapshot_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """Return a recursively read-only, detached mapping."""

    return MappingProxyType(
        {key: freeze_snapshot_value(item) for key, item in value.items()}
    )
