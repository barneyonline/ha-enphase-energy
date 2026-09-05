"""Tests for device registry compatibility helpers."""

from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.enphase_ev.device_registry_compat import (
    device_belongs_to_config_entry,
    device_config_entry_ids,
    get_device_by_identifier,
)


def test_scoped_identifier_lookup_uses_modern_registry_api() -> None:
    """Modern Home Assistant lookups include the owning config entry."""

    device = SimpleNamespace(id="owned-device")
    scoped_lookup = Mock(return_value=device)
    registry = SimpleNamespace(async_get_device_by_identifier=scoped_lookup)

    assert (
        get_device_by_identifier(registry, ("enphase_ev", "shared"), "entry-a")
        is device
    )
    scoped_lookup.assert_called_once_with(("enphase_ev", "shared"), "entry-a")


def test_device_config_entry_ids_support_modern_legacy_and_composite_devices() -> None:
    """Ordinary devices use one owner while legacy composites retain all owners."""

    assert device_config_entry_ids(SimpleNamespace(config_entry_id="entry-a")) == (
        "entry-a",
    )
    assert set(
        device_config_entry_ids(
            SimpleNamespace(
                config_entry_id=None,
                config_entries={"entry-a", "entry-b"},
            )
        )
    ) == {"entry-a", "entry-b"}
    assert device_config_entry_ids(SimpleNamespace(config_entry_id=None)) == ()
    composite = SimpleNamespace(
        id="old-composite-id",
        config_entry_id="primary-entry",
        config_entries={"entry-a", "entry-b"},
    )
    composite_registry = SimpleNamespace(
        async_get_devices_for_composite_device_id=lambda device_id: (
            [
                SimpleNamespace(config_entry_id="entry-a"),
                SimpleNamespace(config_entry_id="entry-b"),
            ]
            if device_id == composite.id
            else []
        )
    )
    assert set(
        device_config_entry_ids(composite, device_registry=composite_registry)
    ) == {"entry-a", "entry-b"}
    ordinary_registry = SimpleNamespace(
        async_get_devices_for_composite_device_id=lambda _device_id: []
    )
    assert device_config_entry_ids(
        SimpleNamespace(id="ordinary", config_entry_id="entry-a"),
        device_registry=ordinary_registry,
    ) == ("entry-a",)
    assert device_belongs_to_config_entry(
        SimpleNamespace(config_entry_id="entry-a"), "entry-a"
    )
    assert not device_belongs_to_config_entry(
        SimpleNamespace(config_entry_id="entry-a"), "entry-b"
    )
    assert not device_belongs_to_config_entry(
        SimpleNamespace(config_entry_id="entry-a"), None
    )
