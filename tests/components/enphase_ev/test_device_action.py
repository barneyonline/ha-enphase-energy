"""Tests for the device action helpers."""

from __future__ import annotations

import sys
from datetime import timedelta
from importlib import import_module
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
import voluptuous as vol

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN
from homeassistant.helpers import device_registry as dr

from custom_components.enphase_ev.runtime_data import EnphaseRuntimeData
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID

# Provide a lightweight shim for the device automation constants to avoid
# importing the full Home Assistant stack during unit tests.
shim = ModuleType("homeassistant.components.device_automation.const")
shim.CONF_TYPE = "type"
sys.modules["homeassistant.components.device_automation.const"] = shim

device_action = import_module("custom_components.enphase_ev.device_action")
const_module = import_module("custom_components.enphase_ev.const")
DOMAIN = const_module.DOMAIN

CONF_TYPE = "type"


@pytest.fixture
def device_id(hass, config_entry) -> str:
    """Create a charger device linked to the config entry."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, RANDOM_SERIAL), (DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
        name="Garage Charger",
    )
    return device.id


def _make_coordinator() -> SimpleNamespace:
    """Return a coordinator stub with the attributes device actions rely on."""

    class DummyCoord(SimpleNamespace):
        pass

    coord = DummyCoord()
    coord.serials = {RANDOM_SERIAL}
    coord.data = {RANDOM_SERIAL: {}}
    coord.site_id = RANDOM_SITE_ID
    coord.update_interval = timedelta(seconds=30)
    coord.phase_timings = {}

    coord.async_start_charging = AsyncMock(return_value={"status": "ok"})
    coord.async_stop_charging = AsyncMock(return_value=None)

    return coord


@pytest.mark.asyncio
async def test_async_get_actions_returns_start_stop(
    hass, config_entry, device_id
) -> None:
    """Device actions should expose start/stop toggles for charger devices."""
    actions = await device_action.async_get_actions(hass, device_id)
    assert len(actions) == 2
    assert {(action[CONF_DEVICE_ID], action[CONF_TYPE]) for action in actions} == {
        (device_id, device_action.ACTION_START),
        (device_id, device_action.ACTION_STOP),
    }


@pytest.mark.asyncio
async def test_async_get_actions_returns_empty_when_device_missing(hass) -> None:
    """Missing devices should yield no actions."""
    actions = await device_action.async_get_actions(hass, "no-such-device")
    assert actions == []


@pytest.mark.asyncio
async def test_async_get_actions_requires_charger_identifier(
    hass, config_entry
) -> None:
    """Devices with only site identifiers should be ignored."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
        name="Site Device",
    )

    actions = await device_action.async_get_actions(hass, device.id)
    assert actions == []


@pytest.mark.asyncio
async def test_async_get_actions_ignores_type_devices(hass, config_entry) -> None:
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"type:{RANDOM_SITE_ID}:iqevse")},
        manufacturer="Enphase",
        name="EV Chargers (1)",
    )

    actions = await device_action.async_get_actions(hass, device.id)
    assert actions == []


@pytest.mark.asyncio
async def test_async_call_action_handles_missing_device(hass) -> None:
    """Early exit when the referenced device is not found."""
    await device_action.async_call_action_from_config(
        hass,
        {
            CONF_DEVICE_ID: "missing",
            CONF_TYPE: device_action.ACTION_START,
        },
        {},
        None,
    )


@pytest.mark.asyncio
async def test_async_call_action_requires_serial_identifier(hass, config_entry) -> None:
    """If a device lacks a charger serial, the action should do nothing."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"site:{RANDOM_SITE_ID}")},
        manufacturer="Enphase",
        name="Site Device",
    )
    coord = _make_coordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    await device_action.async_call_action_from_config(
        hass,
        {
            CONF_DEVICE_ID: device.id,
            CONF_TYPE: device_action.ACTION_START,
        },
        {},
        None,
    )

    assert coord.async_start_charging.await_count == 0


@pytest.mark.asyncio
async def test_async_call_action_skips_type_identifier_before_serial(
    hass, config_entry
) -> None:
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={
            (DOMAIN, f"type:{RANDOM_SITE_ID}:iqevse"),
            (DOMAIN, RANDOM_SERIAL),
        },
        manufacturer="Enphase",
        name="Garage Charger",
    )
    coord = _make_coordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    await device_action.async_call_action_from_config(
        hass,
        {CONF_DEVICE_ID: device.id, CONF_TYPE: device_action.ACTION_STOP},
        {},
        None,
    )

    coord.async_stop_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_async_call_action_skips_type_identifier_with_ordered_identifiers(
    hass, config_entry, monkeypatch
) -> None:
    fake_device = SimpleNamespace(
        identifiers=[
            (DOMAIN, f"type:{RANDOM_SITE_ID}:iqevse"),
            (DOMAIN, RANDOM_SERIAL),
        ]
    )
    fake_registry = SimpleNamespace(async_get=lambda _device_id: fake_device)
    monkeypatch.setattr(device_action.dr, "async_get", lambda _hass: fake_registry)
    coord = _make_coordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    await device_action.async_call_action_from_config(
        hass,
        {CONF_DEVICE_ID: "ordered", CONF_TYPE: device_action.ACTION_STOP},
        {},
        None,
    )

    coord.async_stop_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_async_call_action_ignores_non_domain_identifiers_first(
    hass, config_entry, monkeypatch
) -> None:
    fake_device = SimpleNamespace(
        identifiers=[
            ("other", "value"),
            (DOMAIN, RANDOM_SERIAL),
        ]
    )
    fake_registry = SimpleNamespace(async_get=lambda _device_id: fake_device)
    monkeypatch.setattr(device_action.dr, "async_get", lambda _hass: fake_registry)
    coord = _make_coordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    await device_action.async_call_action_from_config(
        hass,
        {CONF_DEVICE_ID: "ordered-other", CONF_TYPE: device_action.ACTION_STOP},
        {},
        None,
    )

    coord.async_stop_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_async_call_action_handles_missing_coordinator(
    hass, config_entry, device_id
) -> None:
    """The helper should gracefully handle devices without a coordinator."""

    await device_action.async_call_action_from_config(
        hass,
        {
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: device_action.ACTION_STOP,
        },
        {},
        None,
    )


@pytest.mark.asyncio
async def test_async_call_action_start_success(
    hass, config_entry, device_id, monkeypatch
) -> None:
    """Starting a charge session should invoke the coordinator helpers."""
    coord = _make_coordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    config = {
        CONF_DEVICE_ID: device_id,
        CONF_TYPE: device_action.ACTION_START,
        "charging_level": 30,
        "connector_id": 2,
    }

    await device_action.async_call_action_from_config(hass, config, {}, None)

    coord.async_start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, requested_amps=30, connector_id=2
    )


@pytest.mark.asyncio
async def test_async_call_action_start_handles_not_ready(
    hass, config_entry, device_id
) -> None:
    """A not_ready response should flip desired charging back to False."""
    coord = _make_coordinator()
    coord.async_start_charging.return_value = {"status": "not_ready"}
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    await device_action.async_call_action_from_config(
        hass,
        {
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: device_action.ACTION_START,
        },
        {},
        None,
    )

    coord.async_start_charging.assert_awaited_once_with(
        RANDOM_SERIAL, requested_amps=None, connector_id=1
    )


@pytest.mark.asyncio
async def test_async_call_action_stop(hass, config_entry, device_id) -> None:
    """Stopping a charge session should invoke the coordinator client."""
    coord = _make_coordinator()
    config_entry.runtime_data = EnphaseRuntimeData(coordinator=coord)
    object.__setattr__(config_entry, "state", ConfigEntryState.LOADED)

    await device_action.async_call_action_from_config(
        hass,
        {
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: device_action.ACTION_STOP,
        },
        {},
        None,
    )

    coord.async_stop_charging.assert_awaited_once_with(RANDOM_SERIAL)


@pytest.mark.asyncio
async def test_async_get_action_capabilities() -> None:
    """Capabilities schema should expose optional fields for start action."""
    result = await device_action.async_get_action_capabilities(
        None,
        {
            CONF_TYPE: device_action.ACTION_START,
        },
    )
    schema = result["extra_fields"]
    validated = schema(
        {
            "charging_level": 24,
            "connector_id": 1,
        }
    )
    assert validated["charging_level"] == 24
    assert validated["connector_id"] == 1


@pytest.mark.parametrize(
    "action_type, extra",
    [
        ("unknown", {}),
        (device_action.ACTION_START, {"charging_level": 5}),
        (device_action.ACTION_START, {"charging_level": 41}),
        (device_action.ACTION_START, {"charging_level": "not-an-integer"}),
        (device_action.ACTION_START, {"connector_id": 0}),
        (device_action.ACTION_START, {"connector_id": 3}),
        (device_action.ACTION_START, {"connector_id": "not-an-integer"}),
    ],
)
def test_action_schema_rejects_invalid_config(action_type, extra) -> None:
    """Invalid device action types and fields should fail validation."""
    with pytest.raises(vol.Invalid):
        device_action.ACTION_SCHEMA(
            {
                CONF_DEVICE_ID: "device-id",
                CONF_DOMAIN: DOMAIN,
                CONF_TYPE: action_type,
                **extra,
            }
        )


def test_action_schema_validates_start_defaults() -> None:
    """The action schema should validate and default a start action."""
    validated = device_action.ACTION_SCHEMA(
        {
            CONF_DEVICE_ID: "device-id",
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: device_action.ACTION_START,
            "charging_level": 24,
        }
    )

    assert validated["charging_level"] == 24
    assert validated["connector_id"] == 1


def test_action_schema_rejects_start_fields_on_stop() -> None:
    """Start-only options should not be accepted by the stop action."""
    with pytest.raises(vol.Invalid):
        device_action.ACTION_SCHEMA(
            {
                CONF_DEVICE_ID: "device-id",
                CONF_DOMAIN: DOMAIN,
                CONF_TYPE: device_action.ACTION_STOP,
                "connector_id": 1,
            }
        )
