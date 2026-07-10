from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_DEVICE_ID, CONF_TYPE
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .runtime_data import iter_coordinators

ACTION_START = "start_charging"
ACTION_STOP = "stop_charging"
CONF_CHARGING_LEVEL = "charging_level"
CONF_CONNECTOR_ID = "connector_id"

ACTION_SCHEMA = vol.Any(
    cv.DEVICE_ACTION_BASE_SCHEMA.extend(
        {
            vol.Required(CONF_TYPE): ACTION_START,
            vol.Optional(CONF_CHARGING_LEVEL): vol.All(int, vol.Range(min=6, max=40)),
            vol.Optional(CONF_CONNECTOR_ID, default=1): vol.All(
                int, vol.Range(min=1, max=2)
            ),
        }
    ),
    cv.DEVICE_ACTION_BASE_SCHEMA.extend(
        {
            vol.Required(CONF_TYPE): ACTION_STOP,
        }
    ),
)


async def async_get_actions(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if not device:
        return actions
    if not any(
        domain == DOMAIN
        and not ident.startswith("site:")
        and not ident.startswith("type:")
        for domain, ident in device.identifiers
    ):
        return actions

    for typ in (ACTION_START, ACTION_STOP):
        actions.append({CONF_DEVICE_ID: device_id, CONF_TYPE: typ, "domain": DOMAIN})
    return actions


async def async_call_action_from_config(
    hass: HomeAssistant,
    config: ConfigType,
    variables: dict[str, Any],
    context: Context | None,
) -> None:
    typ = config[CONF_TYPE]
    device_id = config[CONF_DEVICE_ID]

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if not device:
        return
    # Resolve serial and coordinator
    sn = None
    for domain, ident in device.identifiers:
        if domain != DOMAIN:
            continue
        if ident.startswith("site:") or ident.startswith("type:"):
            continue
        sn = ident
        break
    if not sn:
        return
    coord = None
    for candidate in iter_coordinators(hass):
        if (
            not candidate.serials
            or sn in candidate.serials
            or sn in (candidate.data or {})
        ):
            coord = candidate
            break
    if not coord:
        return

    if typ == ACTION_START:
        level = config.get(CONF_CHARGING_LEVEL)
        connector_id = config.get(CONF_CONNECTOR_ID, 1)
        await coord.async_start_charging(
            sn, requested_amps=level, connector_id=connector_id
        )
        return

    if typ == ACTION_STOP:
        await coord.async_stop_charging(sn)
        return

    # Amps are read-only; no set action


async def async_get_action_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, Any]:
    typ = config[CONF_TYPE]
    fields: dict[object, object] = {}
    if typ in (ACTION_START,):
        fields[vol.Optional(CONF_CHARGING_LEVEL, default=32)] = vol.All(
            int, vol.Range(min=6, max=40)
        )
    if typ == ACTION_START:
        fields[vol.Optional(CONF_CONNECTOR_ID, default=1)] = vol.All(
            int, vol.Range(min=1, max=2)
        )
    return {"extra_fields": vol.Schema(fields) if fields else vol.Schema({})}
