"""Shared onboarding and options policies and discovery normalization."""

from __future__ import annotations
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from .const import (
    DOMAIN,
)
from .device_types import (
    member_is_retired,
)
from .inventory_payload_helpers import hems_devices_groups

_hems_devices_groups = hems_devices_groups

if TYPE_CHECKING:
    import aiohttp
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CONFIG_ENTRY_MINOR_VERSION = 3

MFA_RESEND_DELAY_SECONDS = 30

CONF_OTP = "otp"

CONF_RESEND_CODE = "resend_code"

CONF_TYPE_ENVOY = "type_envoy"

CONF_TYPE_ENCHARGE = "type_encharge"

CONF_TYPE_AC_BATTERY = "type_ac_battery"

CONF_TYPE_IQEVSE = "type_iqevse"

CONF_TYPE_HEATPUMP = "type_heatpump"

CONF_TYPE_MICROINVERTER = "type_microinverter"

CONF_DEVICE_CATEGORIES_SECTION = "devices"


def _load_get_clientsession() -> Callable[[HomeAssistant], aiohttp.ClientSession]:
    """Load the Home Assistant session factory outside the event loop."""

    from homeassistant.helpers.aiohttp_client import (
        async_get_clientsession as get_clientsession,
    )

    return cast("Callable[[HomeAssistant], aiohttp.ClientSession]", get_clientsession)


async def async_get_clientsession(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Return the shared client session without loading it at import time."""

    get_clientsession = await hass.async_add_import_executor_job(
        _load_get_clientsession
    )
    return get_clientsession(hass)


CONF_DEVICE_FEATURES_SECTION = "device_features"

CONF_MIGRATION_SOURCE_ENTRY = "selected_envoy_source"

CONF_MIGRATION_BACKUP_CONFIRMED = "backup_confirmed"

CONF_MIGRATION_CONFIRM_REASSIGN = "confirm_reassign"

CONF_MIGRATION_DISABLE_ARCHIVED = "disable_archived_envoy_sensors"

CONF_GRID_PROFILE_REGION = "grid_profile_region"

CONF_GRID_PROFILE_COMMONLY_USED = "grid_profile_commonly_used"

CONF_GRID_PROFILE_ID = "grid_profile_id"

CONF_GRID_PROFILE_CONFIRM_APPLY = "confirm_apply"

CONF_GRID_MODE = "mode"

CONF_GRID_MODE_CONFIRM = "confirm"

_GRID_PROFILE_LABEL_PREFIX = f"component.{DOMAIN}.selector.grid_profile_status.options."

_GRID_MODE_LABEL_PREFIX = f"component.{DOMAIN}.selector.grid_mode.options."

_GRID_CONTROL_BLOCK_REASON_LABEL_PREFIX = (
    f"component.{DOMAIN}.selector.grid_control_block_reason.options."
)

_TYPE_FIELD_BY_KEY: dict[str, str] = {
    "envoy": CONF_TYPE_ENVOY,
    "encharge": CONF_TYPE_ENCHARGE,
    "ac_battery": CONF_TYPE_AC_BATTERY,
    "iqevse": CONF_TYPE_IQEVSE,
    "heatpump": CONF_TYPE_HEATPUMP,
    "microinverter": CONF_TYPE_MICROINVERTER,
}


def _battery_site_settings_has_acb(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if isinstance(data, dict):
        payload = data
    value = payload.get("hasAcb")
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    try:
        text = str(value).strip().lower()
    except Exception:  # noqa: BLE001
        return False
    return text in {"1", "true", "yes", "on"}


def _site_entry_title(site_id: str) -> str:
    return f"Site: {site_id}"


def _coerce_int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int | None:
    number = _coerce_int_value(value)
    if number is None:
        return None
    if minimum <= number <= maximum:
        return number
    return None


def _clamped_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    number = _coerce_int_value(value)
    if number is None:
        return default
    return min(maximum, max(minimum, number))


def _hems_heatpump_available(payload: object) -> bool:
    """Return True when dedicated HEMS inventory exposes active heat-pump members."""

    for grouped in _hems_devices_groups(payload):
        for key in ("heat-pump", "heat_pump", "heatpump"):
            members = grouped.get(key)
            if not isinstance(members, list):
                continue
            if any(
                isinstance(member, dict) and not member_is_retired(member)
                for member in members
            ):
                return True
    return False


def _legacy_microinverters_available(payload: object) -> bool:
    """Return True when legacy inverter inventory exposes active members."""

    if not isinstance(payload, dict):
        return False
    inverters = payload.get("inverters")
    if not isinstance(inverters, list):
        result = payload.get("result")
        if isinstance(result, dict):
            inverters = result.get("inverters")
    if not isinstance(inverters, list):
        return False
    return any(
        isinstance(member, dict) and not member_is_retired(member)
        for member in inverters
    )
