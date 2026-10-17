"""Service target expansion uses the supported Home Assistant API once."""

from unittest.mock import Mock

import pytest
from homeassistant.core import ServiceCall
from homeassistant.helpers import device_registry as dr, entity_registry as er, target
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev.const import DOMAIN
from custom_components.enphase_ev.service_routing import ServiceRouter


@pytest.mark.asyncio
@pytest.mark.parametrize("resolve_site", [False, True])
async def test_service_routing_expands_targets_once(hass, monkeypatch, resolve_site):
    """Merge entity devices with explicit targets without expanding twice."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    devices = [
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, serial)}
        )
        for serial in ("entity-device", "direct-device")
    ]
    entity = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "charger-status", config_entry=entry, device_id=devices[0].id
    )
    extractor = Mock(wraps=target.async_extract_referenced_entity_ids)
    monkeypatch.setattr(target, "async_extract_referenced_entity_ids", extractor)
    call = ServiceCall(
        hass,
        DOMAIN,
        "stop_charging",
        {
            "entity_id": entity.entity_id,
            "device_id": [devices[1].id, "missing-device"],
            "site_id": "selected-site",
        },
    )

    router = ServiceRouter(hass)
    if resolve_site:
        assert await router._resolve_site_ids_from_call(call) == {"selected-site"}
    else:
        assert set(router._extract_device_ids(call)) == {
            devices[0].id,
            devices[1].id,
            "missing-device",
        }
    extractor.assert_called_once()
