from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

try:
    from homeassistant.components.automation.triggers import state as state_trigger
except ModuleNotFoundError:
    from homeassistant.components.homeassistant.triggers import state as state_trigger
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

TRIGGER_MAP: dict[str, dict[str, Any]] = {
    # Mapping: tkey is the binary_sensor translation key, to/from are states.
    "charging_started": {"tkey": "charging", "to": STATE_ON, "from": STATE_OFF},
    "charging_stopped": {"tkey": "charging", "to": STATE_OFF, "from": STATE_ON},
    "plugged_in": {"tkey": "plugged_in", "to": STATE_ON},
    "unplugged": {"tkey": "plugged_in", "to": STATE_OFF},
}


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Return a list of triggers for a device."""
    ent_reg = er.async_get(hass)
    out: list[dict[str, Any]] = []
    # Look up binary_sensor entities for this device and match by translation_key
    by_tkey: dict[str, str] = {}
    for ent in er.async_entries_for_device(ent_reg, device_id):
        if ent.domain != "binary_sensor":
            continue
        if not ent.translation_key:
            continue
        by_tkey[ent.translation_key] = ent.entity_id

    for t, meta in TRIGGER_MAP.items():
        if meta["tkey"] in by_tkey:
            out.append(
                {
                    "platform": "device",
                    "domain": DOMAIN,
                    "device_id": device_id,
                    "type": t,
                    # Include entity to aid frontend
                    "entity_id": by_tkey[meta["tkey"]],
                }
            )
    return out


async def async_attach_trigger(
    hass: HomeAssistant,
    config: dict[str, Any],
    action: Callable[[dict[str, Any]], Awaitable[None]],
    automation_info: dict[str, Any],
) -> CALLBACK_TYPE:
    """Attach a state trigger for the selected device trigger type."""
    ent_reg = er.async_get(hass)
    device_id = config["device_id"]
    trig_type = config.get("type")
    meta = TRIGGER_MAP.get(str(trig_type))
    if not meta:
        # No-op for unknown type
        return lambda: None
    # Find matching entity again in case it changed
    entity_id = None
    for ent in er.async_entries_for_device(ent_reg, device_id):
        if ent.domain == "binary_sensor" and ent.translation_key == meta["tkey"]:
            entity_id = ent.entity_id
            break
    if not entity_id:
        return lambda: None

    state_cfg: dict[str, Any] = {
        "platform": "state",
        "entity_id": entity_id,
        "to": meta["to"],
    }
    if meta.get("from"):
        state_cfg["from"] = meta["from"]

    return cast(
        CALLBACK_TYPE,
        await state_trigger.async_attach_trigger(
            hass, state_cfg, action, automation_info, platform_type="device"
        ),
    )
