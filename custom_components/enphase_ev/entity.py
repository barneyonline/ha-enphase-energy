from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import time as dt_time
from typing import Any, TypeVar, cast

from homeassistant.core import callback as ha_callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnphaseCoordinator
from .device_info_helpers import (
    _compose_charger_model_display,
    _is_redundant_model_id,
    _normalize_evse_display_name,
    _normalize_evse_model_name,
)
from .log_redaction import redact_identifier, redact_text

_LOGGER = logging.getLogger(__name__)

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])
callback = cast(Callable[[_CallbackT], _CallbackT], ha_callback)


def evse_resolved_charge_mode(coord: EnphaseCoordinator, serial: str) -> str | None:
    """Return the best available resolved charge mode for an EVSE."""

    resolve_pref = getattr(coord, "_resolve_charge_mode_pref", None)
    if callable(resolve_pref):
        try:
            mode = resolve_pref(serial)
        except Exception:  # noqa: BLE001
            mode = None
        if isinstance(mode, str):
            normalized = mode.strip().upper()
            if normalized:
                return normalized

    try:
        data = (coord.data or {}).get(serial, {})
    except Exception:  # noqa: BLE001
        data = {}
    for key in ("charge_mode_pref", "charge_mode"):
        value = data.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.strip().upper()
        if normalized:
            return normalized
    return None


def evse_amp_control_applicable(coord: EnphaseCoordinator, serial: str) -> bool:
    """Return whether direct amp control applies for the charger's current mode."""

    return evse_resolved_charge_mode(coord, serial) not in {
        "GREEN_CHARGING",
        "SMART_CHARGING",
    }


def battery_schedule_extra_state_attributes(
    coord: object,
    *,
    start_time: dt_time | None = None,
    end_time: dt_time | None = None,
    schedule_status: str | None = None,
    schedule_pending: bool | None = None,
    schedule_enabled: bool | None = None,
    schedule_limit: int | None = None,
) -> dict[str, object]:
    """Return shared battery schedule attributes for UI-facing entities."""

    attrs: dict[str, object] = {
        "write_pending": bool(getattr(coord, "battery_settings_write_pending", False)),
        "write_age_seconds": getattr(coord, "battery_settings_write_age_seconds", None),
    }
    if start_time is not None:
        attrs["start_time"] = start_time.isoformat(timespec="minutes")
    if end_time is not None:
        attrs["end_time"] = end_time.isoformat(timespec="minutes")
    if schedule_status is not None:
        attrs["schedule_status"] = schedule_status
    if schedule_pending is not None:
        attrs["schedule_pending"] = schedule_pending
    if schedule_enabled is not None:
        attrs["schedule_enabled"] = schedule_enabled
    if schedule_limit is not None:
        attrs["schedule_limit"] = schedule_limit
    return attrs


class EnphaseBaseEntity(
    CoordinatorEntity[EnphaseCoordinator],  # type: ignore[misc]
):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EnphaseCoordinator, serial: str) -> None:
        super().__init__(coordinator, context=serial)
        self._coord = coordinator
        self._sn = serial
        self._data: dict[str, Any] = {}
        self._has_data = False
        self._unavailable_logged = False
        self._ever_had_data = False
        source = coordinator.data or {}
        if isinstance(source, dict):
            self._has_data = serial in source
            if self._has_data:
                self._data = source.get(serial) or {}
                self._ever_had_data = True

    @property
    def available(self) -> bool:
        return super().available and self._has_data

    @property
    def data(self) -> dict[str, Any]:
        if hasattr(self, "_data"):
            return self._data
        source = getattr(self, "_coord", None)
        if source is not None:
            try:
                coord_data = source.data or {}
            except AttributeError:
                coord_data = {}
            if isinstance(coord_data, dict):
                return coord_data.get(getattr(self, "_sn", ""), {}) or {}
        return {}

    @callback
    def _handle_coordinator_update(self) -> None:
        source = self._coord.data or {}
        prev_has_data = self._has_data
        self._has_data = self._sn in source
        self._data = source.get(self._sn) or {}
        if self._has_data:
            self._ever_had_data = True
            if self._unavailable_logged:
                _LOGGER.info(
                    "Enphase charger %s data available again",
                    redact_identifier(self._sn),
                )
                self._unavailable_logged = False
        else:
            if self._ever_had_data and not self._unavailable_logged and prev_has_data:
                last_error = getattr(self._coord, "_last_error", None)
                if last_error:
                    _LOGGER.info(
                        "Enphase charger %s data unavailable (%s)",
                        redact_identifier(self._sn),
                        redact_text(
                            last_error,
                            site_ids=(self._coord.site_id,),
                            identifiers=(self._sn,),
                        ),
                    )
                else:
                    _LOGGER.info(
                        "Enphase charger %s data unavailable",
                        redact_identifier(self._sn),
                    )
                self._unavailable_logged = True
        super()._handle_coordinator_update()

    @property
    def device_info(self) -> DeviceInfo:
        d = self.data
        display_name = _normalize_evse_display_name(
            d.get("display_name")
            if d.get("display_name") is not None
            else d.get("name")
        )
        model_name_raw = d.get("model_name")
        model_name = _normalize_evse_model_name(model_name_raw)

        if display_name:
            dev_name = display_name
        elif model_name:
            dev_name = model_name
        else:
            dev_name = "Enphase Energy Charger"

        model_display = _compose_charger_model_display(display_name, model_name_raw)
        # Build DeviceInfo using keyword arguments as per HA dev docs
        info_kwargs: dict[str, object] = {
            "identifiers": {(DOMAIN, self._sn)},
            "manufacturer": "Enphase",
            "name": dev_name,
            "serial_number": str(self._sn),
        }
        # Optional enrichment when available
        if model_display:
            info_kwargs["model"] = model_display
        model_id_value = d.get("model_id")
        if model_id_value and not _is_redundant_model_id(
            info_kwargs.get("model"), model_id_value
        ):
            info_kwargs["model_id"] = str(model_id_value)
        if d.get("hw_version"):
            info_kwargs["hw_version"] = str(d.get("hw_version"))
        if d.get("sw_version"):
            info_kwargs["sw_version"] = str(d.get("sw_version"))
        mac_address = d.get("mac_address")
        if mac_address is not None:
            try:
                mac_clean = str(mac_address).strip().lower().replace("-", ":")
            except Exception:  # noqa: BLE001
                mac_clean = None
            if mac_clean:
                info_kwargs["connections"] = {(CONNECTION_NETWORK_MAC, mac_clean)}
        return DeviceInfo(**info_kwargs)
