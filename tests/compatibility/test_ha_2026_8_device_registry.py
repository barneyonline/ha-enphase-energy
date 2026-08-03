"""Compatibility contract tests for Home Assistant 2026.8 device registry APIs."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

DOMAIN = "enphase_ev"

pytestmark = pytest.mark.skipif(
    not hasattr(dr.DeviceRegistry, "async_get_device_by_identifier"),
    reason="Home Assistant 2026.8 device registry API is unavailable",
)


def test_config_entry_scoped_device_identifiers(hass: HomeAssistant) -> None:
    """The same identifier resolves independently for separate config entries."""
    first_entry = MockConfigEntry(domain=DOMAIN, unique_id="first-site")
    second_entry = MockConfigEntry(domain=DOMAIN, unique_id="second-site")
    first_entry.add_to_hass(hass)
    second_entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    shared_identifier = (DOMAIN, "shared-serial")

    first_device = registry.async_get_or_create(
        config_entry_id=first_entry.entry_id,
        identifiers={shared_identifier},
        name="First charger",
    )
    second_device = registry.async_get_or_create(
        config_entry_id=second_entry.entry_id,
        identifiers={shared_identifier},
        name="Second charger",
    )

    assert first_device.id != second_device.id
    assert first_device.config_entry_id == first_entry.entry_id
    assert second_device.config_entry_id == second_entry.entry_id
    assert (
        registry.async_get_device_by_identifier(shared_identifier, first_entry.entry_id)
        == first_device
    )
    assert (
        registry.async_get_device_by_identifier(
            shared_identifier, second_entry.entry_id
        )
        == second_device
    )


def test_via_device_id_links_scoped_devices(hass: HomeAssistant) -> None:
    """The 2026.8 via_device_id keyword links a child to its exact parent."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="site")
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    parent = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "site:site")},
        name="Site",
    )

    child = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "charger")},
        name="Charger",
        via_device_id=parent.id,
    )

    assert child.via_device_id == parent.id
