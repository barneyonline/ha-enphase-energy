"""Tests for device ownership and composite registry helpers."""

from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.device_registry_helpers import (
    device_belongs_to_config_entry,
    device_config_entry_ids,
)


@pytest.mark.asyncio
async def test_real_devices_use_scoped_lookup_and_singular_owner(hass, monkeypatch):
    """Ordinary registry devices must never read deprecated plural ownership."""
    registry = dr.async_get(hass)
    entries = [MockConfigEntry(domain=DOMAIN, data={}) for _ in range(2)]
    for entry in entries:
        entry.add_to_hass(hass)
    identifier = (DOMAIN, "shared-device")
    devices = [
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={identifier}
        )
        for entry in entries
    ]
    assert devices[0].id != devices[1].id

    def forbidden_plural_owner(_device):
        raise AssertionError("Ordinary devices must use config_entry_id")

    with monkeypatch.context() as patcher:
        patcher.setattr(
            dr.DeviceEntry, "config_entries", property(forbidden_plural_owner)
        )
        for index, (entry, device) in enumerate(zip(entries, devices, strict=True)):
            assert (
                registry.async_get_device_by_identifier(identifier, entry.entry_id)
                is device
            )
            for supplied_registry in (None, registry):
                assert device_config_entry_ids(
                    device, device_registry=supplied_registry
                ) == (entry.entry_id,)
            assert device_belongs_to_config_entry(device, entry.entry_id)
            assert not device_belongs_to_config_entry(
                device, entries[1 - index].entry_id
            )


def test_device_config_entry_ids_support_singular_and_composite_owners() -> None:
    """Ordinary devices use one owner while legacy composites retain all owners."""

    assert device_config_entry_ids(SimpleNamespace(config_entry_id="entry-a")) == (
        "entry-a",
    )
    assert device_config_entry_ids(SimpleNamespace(config_entry_id=None)) == ()
    composite = SimpleNamespace(
        id="old-composite-id",
        config_entry_id="primary-entry",
    )
    composite_registry = SimpleNamespace(
        async_get_devices_for_composite_device_id=lambda device_id: (
            [
                SimpleNamespace(config_entry_id="entry-a"),
                SimpleNamespace(config_entry_id="entry-b"),
                SimpleNamespace(config_entry_id="entry-a"),
            ]
            if device_id == composite.id
            else []
        )
    )
    assert device_config_entry_ids(composite, device_registry=composite_registry) == (
        "entry-a",
        "entry-b",
    )
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
