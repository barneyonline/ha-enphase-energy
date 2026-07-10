"""Tests for device triggers."""

from __future__ import annotations

import sys
from importlib import import_module, reload
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID

triggers_pkg = ModuleType("homeassistant.components.automation.triggers")
state_mod = ModuleType("homeassistant.components.automation.triggers.state")


async def _placeholder_async_attach_trigger(*args, **kwargs):
    raise NotImplementedError


state_mod.async_attach_trigger = _placeholder_async_attach_trigger
triggers_pkg.state = state_mod
sys.modules.setdefault(
    "homeassistant.components.automation",
    ModuleType("homeassistant.components.automation"),
)
sys.modules["homeassistant.components.automation.triggers"] = triggers_pkg
sys.modules["homeassistant.components.automation.triggers.state"] = state_mod
sys.modules["homeassistant.components.automation"].triggers = triggers_pkg

device_trigger = import_module("custom_components.enphase_ev.device_trigger")
const_module = import_module("custom_components.enphase_ev.const")
DOMAIN = const_module.DOMAIN


def test_device_trigger_import_falls_back_to_homeassistant_state_trigger():
    """Use the Home Assistant trigger module path when automation triggers are absent."""
    fallback_pkg = ModuleType("homeassistant.components.homeassistant.triggers")
    fallback_state_mod = ModuleType(
        "homeassistant.components.homeassistant.triggers.state"
    )
    fallback_pkg.state = fallback_state_mod

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setitem(
            sys.modules,
            "homeassistant.components.automation",
            ModuleType("homeassistant.components.automation"),
        )
        monkeypatch.delitem(
            sys.modules,
            "homeassistant.components.automation.triggers",
            raising=False,
        )
        monkeypatch.delitem(
            sys.modules,
            "homeassistant.components.automation.triggers.state",
            raising=False,
        )
        monkeypatch.setitem(
            sys.modules,
            "homeassistant.components.homeassistant.triggers",
            fallback_pkg,
        )
        monkeypatch.setitem(
            sys.modules,
            "homeassistant.components.homeassistant.triggers.state",
            fallback_state_mod,
        )

        assert reload(device_trigger).state_trigger is fallback_state_mod

    assert reload(device_trigger).state_trigger is state_mod


@pytest.fixture
def device_entry(hass, config_entry):
    """Create a device and matching binary sensors for trigger discovery."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL), (DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
        name="Garage Charger",
    )

    ent_reg = er.async_get(hass)
    entity_ids: list[str] = []
    for tkey, unique in (
        ("charging", "charging-bin"),
        ("plugged_in", "plug-bin"),
    ):
        entry = ent_reg.async_get_or_create(
            domain="binary_sensor",
            platform="enphase_ev",
            unique_id=unique,
            device_id=device.id,
            config_entry=config_entry,
            translation_key=tkey,
        )
        entity_ids.append(entry.entity_id)
    return device, entity_ids


@pytest.mark.asyncio
async def test_async_get_triggers_exposes_device_triggers(hass, device_entry):
    """All known triggers for the device should be discovered."""
    device, _ = device_entry
    triggers = await device_trigger.async_get_triggers(hass, device.id)
    assert {trigger["type"] for trigger in triggers} == {
        "charging_started",
        "charging_stopped",
        "plugged_in",
        "unplugged",
    }
    assert all(
        trigger["entity_id"].startswith("binary_sensor.") for trigger in triggers
    )


@pytest.mark.asyncio
async def test_async_get_triggers_ignores_unrelated_entries(
    hass, config_entry, device_entry
):
    """Non-binary entities or missing translation keys are skipped."""
    device, _ = device_entry
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        domain="sensor",
        platform="enphase_ev",
        unique_id="sensor-entry",
        device_id=device.id,
        config_entry=config_entry,
        translation_key=None,
    )
    ent_reg.async_get_or_create(
        domain="binary_sensor",
        platform="enphase_ev",
        unique_id="no-translation",
        device_id=device.id,
        config_entry=config_entry,
        translation_key=None,
    )

    triggers = await device_trigger.async_get_triggers(hass, device.id)
    assert {trigger["type"] for trigger in triggers} == {
        "charging_started",
        "charging_stopped",
        "plugged_in",
        "unplugged",
    }


@pytest.mark.asyncio
async def test_async_attach_trigger_wraps_state_trigger(
    hass, device_entry, monkeypatch
):
    """Attaching a trigger should delegate to the state trigger helper."""
    async_mock = AsyncMock(return_value="unsubscribe")
    monkeypatch.setattr(
        device_trigger.state_trigger, "async_attach_trigger", async_mock
    )

    action = MagicMock()
    automation_info = {"name": "automation"}

    device, _ = device_entry
    unsubscribe = await device_trigger.async_attach_trigger(
        hass,
        {"device_id": device.id, "type": "charging_started"},
        action,
        automation_info,
    )

    async_mock.assert_awaited_once()
    state_cfg = async_mock.await_args.args[1]
    assert state_cfg["entity_id"].startswith("binary_sensor.")
    assert state_cfg["to"] == "on"
    assert state_cfg["from"] == "off"
    assert unsubscribe == "unsubscribe"


@pytest.mark.asyncio
async def test_async_attach_trigger_handles_missing_entity(hass, device_entry):
    """If the entity is missing, the helper should return a no-op."""
    device, entity_ids = device_entry
    ent_reg = er.async_get(hass)
    for entity_id in entity_ids:
        ent_reg.async_remove(entity_id)

    detach = await device_trigger.async_attach_trigger(
        hass, {"device_id": device.id, "type": "charging_started"}, None, {}
    )
    assert callable(detach)
    assert detach() is None


@pytest.mark.asyncio
async def test_async_attach_trigger_rejects_unknown_type(hass, device_entry):
    """Unknown trigger types should be rejected instead of becoming a no-op."""
    device, _ = device_entry
    with pytest.raises(HomeAssistantError, match="Unhandled trigger type unknown"):
        await device_trigger.async_attach_trigger(
            hass, {"device_id": device.id, "type": "unknown"}, None, {}
        )


def test_trigger_schema_validates_known_trigger() -> None:
    """Known device triggers and their optional entity should validate."""
    config = {
        CONF_PLATFORM: "device",
        CONF_DOMAIN: DOMAIN,
        CONF_DEVICE_ID: "device-id",
        CONF_TYPE: "charging_started",
        "entity_id": "binary_sensor.charging",
    }

    assert device_trigger.TRIGGER_SCHEMA(config) == config


def test_trigger_schema_rejects_unknown_trigger() -> None:
    """Unknown device trigger types should fail validation."""
    with pytest.raises(vol.Invalid):
        device_trigger.TRIGGER_SCHEMA(
            {
                CONF_PLATFORM: "device",
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: "device-id",
                CONF_TYPE: "unknown",
            }
        )
