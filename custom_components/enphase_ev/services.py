from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date, time
from typing import TYPE_CHECKING, Any, NoReturn, cast

import aiohttp
import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import service as ha_service
from homeassistant.helpers import target as ha_target
from homeassistant.helpers.service import async_register_admin_service

from .api import (
    OCPP_TRIGGER_MESSAGES,
    OCPP_TRIGGER_MESSAGES_REQUIRING_CONFIRMATION,
    validate_ocpp_trigger_message,
)
from .battery_schedule_editor import (
    BatteryScheduleRecord,
    battery_schedule_inventory,
    battery_schedule_overlap_message,
    battery_schedule_overlap_placeholders,
    battery_schedule_overlap_record,
)
from .const import (
    DOMAIN,
    ISSUE_AUTH_BLOCKED,
    ISSUE_REAUTH_REQUIRED,
    ISSUE_TOO_MANY_ACTIVE_SESSIONS,
)
from .device_types import parse_type_identifier
from .log_redaction import redact_site_id
from .parsing_helpers import coerce_optional_bool
from .grid_profile_runtime import SUPPORT_DENIED, GridProfileRuntime
from .runtime_data import EnphaseRuntimeData, iter_coordinators
from .service_validation import raise_translated_service_validation

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import EnphaseCoordinator

REGISTERED_SERVICES = (
    "force_refresh",
    "start_charging",
    "stop_charging",
    "trigger_message",
    "request_grid_toggle_otp",
    "set_grid_mode",
    "clear_reauth_issue",
    "clear_hems_auth_backoff",
    "try_reauth_now",
    "start_live_stream",
    "stop_live_stream",
    "sync_schedules",
    "add_schedule",
    "update_schedule",
    "delete_schedule",
    "validate_schedule",
    "update_cfg_schedule",
    "update_tariff",
    "browse_grid_profiles",
    "refresh_grid_profiles",
    "set_grid_profile",
)

_LOGGER = logging.getLogger(__name__)


def async_setup_services(
    hass: HomeAssistant,
    *,
    supports_response: type[SupportsResponse] = SupportsResponse,
) -> None:
    """Register integration services once."""

    if hass.services.has_service(DOMAIN, "start_charging"):
        return

    from homeassistant.exceptions import ServiceValidationError

    SCHEDULE_TYPE_SCHEMA = vol.In(("cfg", "dtg", "rbd"))
    DAYS_SCHEMA = vol.All(
        cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=1, max=7))]
    )
    SCHEDULE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

    def _raise_service_validation(
        key: str,
        *,
        placeholders: dict[str, object] | None = None,
        message: str | None = None,
    ) -> NoReturn:
        raise_translated_service_validation(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders=placeholders,
            message=message,
        )
        raise AssertionError("unreachable")  # pragma: no cover

    def _serial_from_device(dev: dr.DeviceEntry) -> str | None:
        for domain, sn in dev.identifiers:
            if domain == DOMAIN:
                if sn.startswith("site:"):
                    continue
                if sn.startswith("type:"):
                    continue
                return str(sn)
        return None

    def _site_id_from_device(
        dev_reg: dr.DeviceRegistry, dev: dr.DeviceEntry
    ) -> str | None:
        for domain, identifier in dev.identifiers:
            if domain == DOMAIN and identifier.startswith("site:"):
                return str(identifier.partition(":")[2])
            if domain == DOMAIN and identifier.startswith("type:"):
                parsed = parse_type_identifier(identifier)
                if parsed:
                    return str(parsed[0])
        via = dev.via_device_id
        if via:
            parent = dev_reg.async_get(via)
            if parent:
                for domain, identifier in parent.identifiers:
                    if domain == DOMAIN and identifier.startswith("site:"):
                        return str(identifier.partition(":")[2])
                    if domain == DOMAIN and identifier.startswith("type:"):
                        parsed = parse_type_identifier(identifier)
                        if parsed:
                            return str(parsed[0])
        return None

    async def _resolve_site_id(device_id: str) -> str | None:
        dev_reg = dr.async_get(hass)
        dev = dev_reg.async_get(device_id)
        if not dev:
            return None
        return _site_id_from_device(dev_reg, dev)

    def _iter_loaded_coordinators() -> list[EnphaseCoordinator]:
        coordinators: list[EnphaseCoordinator] = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            runtime_data = getattr(entry, "runtime_data", None)
            if isinstance(runtime_data, EnphaseRuntimeData):
                coordinators.append(runtime_data.coordinator)
        return coordinators

    def _coordinator_has_serial(coord: EnphaseCoordinator, sn: str) -> bool:
        data = coord.data if isinstance(getattr(coord, "data", None), dict) else {}
        return sn in (getattr(coord, "serials", None) or set()) or sn in data

    def _coordinator_can_fallback_for_serial(
        coord: EnphaseCoordinator, sn: str, site_id: str | None
    ) -> bool:
        if site_id is not None and str(getattr(coord, "site_id", "")) != site_id:
            return False
        if getattr(coord, "site_only", False):
            return False
        serials = getattr(coord, "serials", None) or set()
        data = coord.data if isinstance(getattr(coord, "data", None), dict) else {}
        return bool(not serials and not data and sn)

    def _device_config_entry_ids(device: dr.DeviceEntry) -> list[str]:
        entry_ids: list[str] = []
        config_entries = getattr(device, "config_entries", None)
        if config_entries:
            entry_ids.extend(str(entry_id) for entry_id in config_entries)
        config_entry_id = getattr(device, "config_entry_id", None)
        if config_entry_id:
            entry_ids.append(str(config_entry_id))
        return list(dict.fromkeys(entry_ids))

    def _config_entry_ids_for_device(
        dev_reg: dr.DeviceRegistry, dev: dr.DeviceEntry
    ) -> list[str]:
        entry_ids = _device_config_entry_ids(dev)
        if entry_ids:
            return entry_ids
        via = dev.via_device_id
        if not via:
            return []
        parent = dev_reg.async_get(via)
        if not parent:
            return []
        return _device_config_entry_ids(parent)

    async def _resolve_device_routing_context(
        device_id: str,
    ) -> tuple[str, str | None, list[str]] | None:
        dev_reg = dr.async_get(hass)
        dev = dev_reg.async_get(device_id)
        if not dev:
            return None
        sn = _serial_from_device(dev)
        if not sn:
            return None
        return (
            sn,
            _site_id_from_device(dev_reg, dev),
            _config_entry_ids_for_device(dev_reg, dev),
        )

    async def _get_coordinator_for_sn(
        sn: str,
        *,
        site_id: str | None = None,
        config_entry_ids: list[str] | None = None,
    ) -> EnphaseCoordinator | None:
        sn = str(sn)
        for entry_id in config_entry_ids or []:
            coord = _get_coordinator_for_entry_id(entry_id)
            if coord is None:
                continue
            if _coordinator_has_serial(coord, sn):
                return coord

        all_coordinators = _iter_loaded_coordinators()
        if site_id is not None:
            site_coordinators = [
                coord
                for coord in all_coordinators
                if str(getattr(coord, "site_id", "")) == site_id
            ]
            exact_matches = [
                coord
                for coord in site_coordinators
                if _coordinator_has_serial(coord, sn)
            ]
            if len(exact_matches) == 1:
                return exact_matches[0]
            if exact_matches:
                return None
            fallback_candidates = [
                coord
                for coord in site_coordinators
                if _coordinator_can_fallback_for_serial(coord, sn, site_id)
            ]
            if len(fallback_candidates) == 1:
                return fallback_candidates[0]
            return None

        exact_matches = [
            coord for coord in all_coordinators if _coordinator_has_serial(coord, sn)
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if exact_matches:
            return None
        fallback_candidates = [
            coord
            for coord in all_coordinators
            if _coordinator_can_fallback_for_serial(coord, sn, None)
        ]
        if len(fallback_candidates) == 1:
            return fallback_candidates[0]
        return None

    def _get_coordinator_for_entry_id(entry_id: str) -> EnphaseCoordinator | None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id != entry_id:
                continue
            runtime_data = getattr(entry, "runtime_data", None)
            if isinstance(runtime_data, EnphaseRuntimeData):
                return runtime_data.coordinator
        return None

    DEVICE_ID_LIST = vol.All(cv.ensure_list, [cv.string])
    ENTRY_SCHEMA: dict[object, object] = {vol.Optional("config_entry_id"): cv.string}

    def _validate_trigger_message(value: object) -> str:
        try:
            return str(validate_ocpp_trigger_message(value))
        except ValueError as err:
            raise vol.Invalid(str(err)) from err

    def _service_trigger_message(value: object) -> str:
        try:
            return str(validate_ocpp_trigger_message(value))
        except ValueError as err:
            _raise_service_validation(
                "trigger_message_invalid",
                placeholders={"messages": ", ".join(sorted(OCPP_TRIGGER_MESSAGES))},
                message=str(err),
            )
        raise AssertionError("unreachable")  # pragma: no cover

    def _confirm_trigger_message(message: str, confirmed: object) -> None:
        if message not in OCPP_TRIGGER_MESSAGES_REQUIRING_CONFIRMATION:
            return
        if confirmed is True:
            return
        _raise_service_validation(
            "trigger_message_confirmation_required",
            placeholders={"message": message},
            message=f"{message} requires confirm_advanced.",
        )

    START_SCHEMA = vol.Schema(
        {
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("charging_level", default=32): vol.All(
                int, vol.Range(min=6, max=40)
            ),
            vol.Optional("connector_id", default=1): vol.All(
                int, vol.Range(min=1, max=2)
            ),
        }
    )
    STOP_SCHEMA = vol.Schema({vol.Optional("device_id"): DEVICE_ID_LIST})
    TRIGGER_SCHEMA = vol.Schema(
        {
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Required("requested_message"): vol.All(
                cv.string, _validate_trigger_message
            ),
            vol.Optional("confirm_advanced", default=False): cv.boolean,
        }
    )
    REQUEST_GRID_OTP_SCHEMA = vol.Schema({vol.Required("config_entry_id"): cv.string})
    SET_GRID_MODE_SCHEMA = vol.Schema(
        {
            vol.Required("config_entry_id"): cv.string,
            vol.Required("mode"): vol.In(("on_grid", "off_grid")),
            vol.Required("otp"): cv.string,
        }
    )
    CLEAR_SCHEMA = vol.Schema(
        {vol.Optional("device_id"): DEVICE_ID_LIST, vol.Optional("site_id"): cv.string}
    )
    SYNC_SCHEMA = vol.Schema({vol.Optional("device_id"): DEVICE_ID_LIST})
    FORCE_REFRESH_SCHEMA = vol.Schema(
        {
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Optional("config_entry_id"): cv.string,
        }
    )
    BROWSE_GRID_PROFILES_SCHEMA = vol.Schema(
        {
            **ENTRY_SCHEMA,
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Optional("region_code"): cv.string,
            vol.Optional("query"): cv.string,
            vol.Optional("commonly_used", default=True): cv.boolean,
        }
    )
    REFRESH_GRID_PROFILES_SCHEMA = vol.Schema(
        {
            **ENTRY_SCHEMA,
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Optional("region_code"): cv.string,
            vol.Optional("commonly_used", default=True): cv.boolean,
        }
    )
    SET_GRID_PROFILE_SCHEMA = vol.Schema(
        {
            **ENTRY_SCHEMA,
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Optional("region_code"): cv.string,
            vol.Optional("gateway_serial"): cv.string,
            vol.Required("profile_id"): cv.string,
            vol.Required("confirm"): cv.boolean,
        }
    )
    ADD_SCHEDULE_SCHEMA = vol.Schema(
        {
            **ENTRY_SCHEMA,
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Required("schedule_type"): SCHEDULE_TYPE_SCHEMA,
            vol.Required("start_time"): cv.time,
            vol.Required("end_time"): cv.time,
            vol.Required("limit"): vol.All(vol.Coerce(int), vol.Range(min=5, max=100)),
            vol.Required("days"): DAYS_SCHEMA,
        }
    )
    UPDATE_SCHEDULE_SCHEMA = vol.Schema(
        {
            **ENTRY_SCHEMA,
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Required("schedule_id"): cv.string,
            vol.Required("schedule_type"): SCHEDULE_TYPE_SCHEMA,
            vol.Required("start_time"): cv.time,
            vol.Required("end_time"): cv.time,
            vol.Required("limit"): vol.All(vol.Coerce(int), vol.Range(min=5, max=100)),
            vol.Required("days"): DAYS_SCHEMA,
            vol.Required("confirm"): cv.boolean,
        }
    )
    DELETE_SCHEDULE_SCHEMA = vol.Schema(
        {
            **ENTRY_SCHEMA,
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Optional("schedule_id"): cv.string,
            vol.Optional("schedule_ids"): cv.ensure_list,
            vol.Optional("schedule_type"): SCHEDULE_TYPE_SCHEMA,
            vol.Required("confirm"): cv.boolean,
        }
    )
    VALIDATE_SCHEDULE_SCHEMA = vol.Schema(
        {
            **ENTRY_SCHEMA,
            vol.Optional("device_id"): DEVICE_ID_LIST,
            vol.Optional("site_id"): cv.string,
            vol.Required("schedule_type"): SCHEDULE_TYPE_SCHEMA,
        }
    )

    def _normalize_schedule_ids(raw: object) -> list[str]:
        schedule_ids: list[str] = []
        if raw is None:
            return schedule_ids
        if isinstance(raw, (list, tuple, set)):
            candidates = [str(value) for value in raw]
        else:
            candidates = re.split(r"[,\s]+", str(raw))
        for candidate in candidates:
            if candidate:
                schedule_ids.append(candidate)
        return [
            schedule_id.strip().strip("'\"")
            for schedule_id in schedule_ids
            if schedule_id.strip().strip("'\"")
        ]

    def _validate_schedule_fields(
        *,
        schedule_type: str,
        start_time: time,
        end_time: time,
        days: list[int],
        limit: int,
    ) -> tuple[str, str]:
        if not days:
            _raise_service_validation(
                "battery_schedule_day_required",
                message="Select at least one day for the schedule.",
            )
        start_str = start_time.strftime("%H:%M")
        end_str = end_time.strftime("%H:%M")
        if start_str == end_str:
            _raise_service_validation(
                "battery_schedule_times_different",
                message="Schedule start and end times must be different.",
            )
        if not 5 <= int(limit) <= 100:
            schedule_type_label = str(schedule_type).upper()
            _raise_service_validation(
                "battery_schedule_limit_range",
                placeholders={
                    "schedule_type": schedule_type_label,
                    "minimum": "5",
                    "maximum": "100",
                },
                message=(
                    f"{schedule_type_label} schedule limit must be between 5 and 100."
                ),
            )
        return start_str, end_str

    async def _validate_schedule_with_api(
        coord: EnphaseCoordinator, schedule_type: str
    ) -> dict[str, object]:
        validator = getattr(coord.client, "validate_battery_schedule", None)
        if not callable(validator):
            return {}
        try:
            result = await validator(schedule_type)
        except aiohttp.ClientResponseError as err:
            if err.status in {403, 404}:
                site_id = getattr(coord, "site_id", None)
                _LOGGER.debug(
                    "Ignoring battery schedule preflight failure for site %s (%s %s)",
                    redact_site_id(site_id),
                    err.status,
                    str(schedule_type).upper(),
                )
                return {}
            raise
        if not isinstance(result, dict):
            return {}
        valid = coerce_optional_bool(result.get("valid"))
        if valid is None and "isValid" in result:
            valid = coerce_optional_bool(result.get("isValid"))
        if valid is False:
            raw_message = result.get("message")
            if isinstance(raw_message, str) and raw_message.strip():
                detail = raw_message.strip()
                _raise_service_validation(
                    "battery_schedule_validation_rejected_detail",
                    placeholders={"message": detail},
                    message=(
                        "Schedule rejected by the Enphase validation endpoint: "
                        f"{detail}"
                    ),
                )
            _raise_service_validation(
                "battery_schedule_validation_rejected",
                message="Schedule rejected by the Enphase validation endpoint.",
            )
        if valid is not None and "valid" not in result:
            return {**result, "valid": bool(valid)}
        return cast(dict[str, object], result)

    def _known_schedule_ids(coord: EnphaseCoordinator) -> set[str]:
        return {schedule.schedule_id for schedule in battery_schedule_inventory(coord)}

    def _schedule_inventory_by_id(
        coord: EnphaseCoordinator,
    ) -> dict[str, BatteryScheduleRecord]:
        return {
            schedule.schedule_id: schedule
            for schedule in battery_schedule_inventory(coord)
        }

    def _remaining_schedule_for_delete_family(
        coord: EnphaseCoordinator,
        schedule_type: str,
        deleted_schedule_ids: set[str],
    ) -> BatteryScheduleRecord | None:
        normalized_schedule_type = str(schedule_type).lower()
        selected_schedule_id = getattr(
            coord, f"_battery_{normalized_schedule_type}_schedule_id", None
        )
        remaining = [
            schedule
            for schedule in battery_schedule_inventory(coord)
            if schedule.schedule_type == normalized_schedule_type
            and schedule.schedule_id not in deleted_schedule_ids
        ]
        if not remaining:
            return None
        if selected_schedule_id is not None:
            for schedule in remaining:
                if schedule.schedule_id == selected_schedule_id:
                    return schedule
        for schedule in remaining:
            if schedule.enabled is True:
                return schedule
        return remaining[0]

    def _apply_schedule_for_update(
        coord: EnphaseCoordinator,
        *,
        schedule_inventory: dict[str, BatteryScheduleRecord],
        schedule_id: str,
        schedule_type: str,
        start_time: str,
        end_time: str,
        enabled: bool | None,
    ) -> tuple[str, str, bool | None]:
        normalized_schedule_type = str(schedule_type).lower()
        selected_schedule_id = getattr(
            coord, f"_battery_{normalized_schedule_type}_schedule_id", None
        )
        if selected_schedule_id is None or selected_schedule_id == schedule_id:
            return start_time, end_time, enabled
        selected_schedule = schedule_inventory.get(str(selected_schedule_id))
        if (
            selected_schedule is not None
            and selected_schedule.schedule_type == normalized_schedule_type
        ):
            return (
                selected_schedule.start_time,
                selected_schedule.end_time,
                selected_schedule.enabled,
            )
        return start_time, end_time, enabled

    def _validate_schedule_overlap(
        coord: EnphaseCoordinator,
        *,
        start_time: str,
        end_time: str,
        days: list[int],
        exclude_schedule_id: str | None = None,
    ) -> None:
        overlapping = battery_schedule_overlap_record(
            coord,
            start_time=start_time,
            end_time=end_time,
            days=days,
            exclude_schedule_id=exclude_schedule_id,
        )
        if overlapping is not None:
            raise_translated_service_validation(
                translation_domain=DOMAIN,
                translation_key="battery_schedule_overlap",
                translation_placeholders=dict(
                    battery_schedule_overlap_placeholders(overlapping, hass=hass)
                ),
                message=battery_schedule_overlap_message(overlapping, hass=hass),
            )

    def _validate_cfg_schedule(data: dict[str, object]) -> dict[str, object]:
        if not any(k in data for k in ("start_time", "end_time", "limit")):
            raise vol.Invalid(
                "At least one of start_time, end_time, or limit must be provided"
            )
        return data

    UPDATE_CFG_SCHEMA = vol.All(
        vol.Schema(
            {
                vol.Optional("device_id"): DEVICE_ID_LIST,
                vol.Optional("site_id"): cv.string,
                vol.Optional("start_time"): cv.time,
                vol.Optional("end_time"): cv.time,
                vol.Optional("limit"): vol.All(
                    vol.Coerce(int), vol.Range(min=5, max=100)
                ),
            }
        ),
        _validate_cfg_schedule,
    )
    TARIFF_BILLING_FIELDS = frozenset(
        {"billing_start_date", "billing_frequency", "billing_interval_value"}
    )
    FRIENDLY_RATE_VALUE_FIELDS = frozenset({"rate", "import_rate", "export_rate"})
    TARIFF_STRUCTURE_FIELDS = frozenset(
        {"tariff_payload", "purchase_tariff", "buyback_tariff"}
    )
    TARIFF_TYPE_SCHEMA = vol.In(("flat", "tou", "tiered"))
    TARIFF_VARIATION_SCHEMA = vol.In(
        ("single", "seasonal", "weekends", "seasonal-and-weekends")
    )
    TARIFF_EXPORT_PLAN_SCHEMA = vol.In(("netFit", "grossFit", "nem"))

    def _validate_tariff_billing_start_date(value: object) -> str:
        text = str(value).strip()
        try:
            date.fromisoformat(text)
        except ValueError as err:
            raise vol.Invalid("billing_start_date must be a valid ISO date") from err
        return text

    def _validate_update_tariff(data: dict[str, object]) -> dict[str, object]:
        rates = data.get("rates") or []
        has_rates = bool(rates) or bool(FRIENDLY_RATE_VALUE_FIELDS.intersection(data))
        provided_billing = TARIFF_BILLING_FIELDS.intersection(data)
        has_structure = bool(
            TARIFF_STRUCTURE_FIELDS.intersection(data)
            or data.get("configure_import_tariff")
            or data.get("configure_export_tariff")
        )
        if "rate_entity" in data and "rate" not in data:
            raise vol.Invalid("Provide both rate_entity and rate")
        for rate_key, entity_key in (
            ("import_rate", "import_rate_entity"),
            ("export_rate", "export_rate_entity"),
        ):
            if (rate_key in data) != (entity_key in data):
                raise vol.Invalid(f"Provide both {entity_key} and {rate_key}")
        if not has_rates and not provided_billing and not has_structure:
            raise vol.Invalid("Provide billing details or at least one rate update")
        if provided_billing and provided_billing != TARIFF_BILLING_FIELDS:
            raise vol.Invalid("Provide all billing fields")
        if provided_billing:
            frequency = data["billing_frequency"]
            interval = int(str(data["billing_interval_value"]))
            maximum = 24 if frequency == "MONTH" else 100
            if interval > maximum:
                raise vol.Invalid(
                    f"billing_interval_value must be between 1 and {maximum}"
                )
        return data

    UPDATE_TARIFF_SCHEMA = vol.All(
        vol.Schema(
            {
                vol.Optional("entity_id"): cv.entity_ids,
                vol.Optional("device_id"): DEVICE_ID_LIST,
                vol.Optional("site_id"): cv.string,
                vol.Optional("config_entry_id"): cv.string,
                vol.Optional("billing_start_date"): _validate_tariff_billing_start_date,
                vol.Optional("billing_frequency"): vol.In(("MONTH", "DAY")),
                vol.Optional("billing_interval_value"): vol.All(
                    vol.Coerce(int), vol.Range(min=1)
                ),
                vol.Optional("rate_entity"): cv.entity_id,
                vol.Optional("rate"): vol.All(vol.Coerce(float), vol.Range(min=0)),
                vol.Optional("import_rate_entity"): cv.entity_id,
                vol.Optional("import_rate"): vol.All(
                    vol.Coerce(float), vol.Range(min=0)
                ),
                vol.Optional("export_rate_entity"): cv.entity_id,
                vol.Optional("export_rate"): vol.All(
                    vol.Coerce(float), vol.Range(min=0)
                ),
                vol.Optional("tariff_payload"): dict,
                vol.Optional("purchase_tariff"): dict,
                vol.Optional("buyback_tariff"): dict,
                vol.Optional("configure_import_tariff"): cv.boolean,
                vol.Optional("import_tariff_type"): TARIFF_TYPE_SCHEMA,
                vol.Optional("import_variation"): TARIFF_VARIATION_SCHEMA,
                vol.Optional("import_flat_rate"): vol.All(
                    vol.Coerce(float), vol.Range(min=0)
                ),
                vol.Optional("import_periods"): vol.All(cv.ensure_list, [dict]),
                vol.Optional("import_tiers"): vol.All(cv.ensure_list, [dict]),
                vol.Optional("import_off_peak_rate"): vol.All(
                    vol.Coerce(float), vol.Range(min=0)
                ),
                vol.Optional("configure_export_tariff"): cv.boolean,
                vol.Optional("export_tariff_type"): TARIFF_TYPE_SCHEMA,
                vol.Optional("export_variation"): TARIFF_VARIATION_SCHEMA,
                vol.Optional("export_plan"): TARIFF_EXPORT_PLAN_SCHEMA,
                vol.Optional("export_flat_rate"): vol.All(
                    vol.Coerce(float), vol.Range(min=0)
                ),
                vol.Optional("export_periods"): vol.All(cv.ensure_list, [dict]),
                vol.Optional("export_tiers"): vol.All(cv.ensure_list, [dict]),
                vol.Optional("export_off_peak_rate"): vol.All(
                    vol.Coerce(float), vol.Range(min=0)
                ),
                vol.Optional("rates"): vol.All(
                    cv.ensure_list,
                    [
                        vol.Schema(
                            {
                                vol.Required("entity_id"): cv.entity_id,
                                vol.Required("rate"): vol.All(
                                    vol.Coerce(float), vol.Range(min=0)
                                ),
                            }
                        )
                    ],
                ),
            }
        ),
        _validate_update_tariff,
    )

    def _extract_device_ids(call: ServiceCall) -> list[str]:
        device_ids: set[str] = set()
        try:
            extractor = getattr(ha_service, "async_extract_referenced_device_ids", None)
            if callable(extractor):
                device_ids |= {str(value) for value in extractor(hass, call)}
        except Exception:
            pass
        data_ids = call.data.get("device_id")
        if data_ids:
            if isinstance(data_ids, str):
                device_ids.add(data_ids)
            else:
                device_ids |= {str(v) for v in data_ids}
        return list(device_ids)

    async def _resolve_site_ids_from_call(call: ServiceCall) -> set[str]:
        site_ids: set[str] = set()
        for device_id in _extract_device_ids(call):
            site_id = await _resolve_site_id(device_id)
            if site_id:
                site_ids.add(site_id)
        explicit = call.data.get("site_id")
        if explicit:
            site_ids.add(str(explicit))
        return site_ids

    async def _resolve_single_site_coordinator(
        call: ServiceCall,
    ) -> EnphaseCoordinator:
        config_entry_id = call.data.get("config_entry_id")
        if config_entry_id:
            coord = _get_coordinator_for_entry_id(str(config_entry_id))
            if coord is not None:
                return coord
        site_ids = await _resolve_site_ids_from_call(call)
        if not site_ids:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="grid_site_required",
            )
        if len(site_ids) > 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="grid_site_ambiguous",
                translation_placeholders={"count": str(len(site_ids))},
            )
        target = next(iter(site_ids))
        coordinators = iter_coordinators(hass, site_ids={target})
        if coordinators:
            return coordinators[0]
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="grid_site_required",
        )

    def _extract_entity_ids(call: ServiceCall) -> list[str]:
        entity_ids: set[str] = set()
        extractor = getattr(ha_target, "async_extract_referenced_entity_ids", None)
        if callable(extractor):
            try:
                entity_ids |= {str(entity_id) for entity_id in extractor(hass, call)}
            except Exception:
                pass
        else:
            extractor = getattr(ha_service, "async_extract_referenced_entity_ids", None)
            if callable(extractor):
                try:
                    entity_ids |= {
                        str(entity_id) for entity_id in extractor(hass, call)
                    }
                except Exception:
                    pass
        raw_entity_ids = call.data.get("entity_id")
        if raw_entity_ids:
            if isinstance(raw_entity_ids, str):
                entity_ids.add(raw_entity_ids)
            else:
                entity_ids |= {str(entity_id) for entity_id in raw_entity_ids}
        return list(entity_ids)

    def _coordinator_from_tariff_entity(
        entity_id: str,
    ) -> EnphaseCoordinator | None:
        ent_reg = er.async_get(hass)
        reg_entry = ent_reg.async_get(entity_id)
        if reg_entry is None:
            return None
        entry_domain = getattr(reg_entry, "domain", entity_id.partition(".")[0])
        if reg_entry.platform != DOMAIN or entry_domain not in {"sensor", "number"}:
            return None
        unique_id = str(reg_entry.unique_id or "")
        if not any(
            token in unique_id
            for token in (
                "_tariff_import_rate_",
                "_tariff_export_rate_",
                "_tariff_current_import_rate",
                "_tariff_current_export_rate",
            )
        ):
            return None
        config_entry_id = getattr(reg_entry, "config_entry_id", None)
        if config_entry_id:
            coord = _get_coordinator_for_entry_id(str(config_entry_id))
            if coord is not None:
                return coord
        for coord in _iter_loaded_coordinators():
            if f"{DOMAIN}_site_{coord.site_id}_" in unique_id:
                return coord
        return None

    def _tariff_rate_update_from_entity(
        entity_id: str,
        rate: float,
        *,
        branch: str | None = None,
    ) -> tuple[EnphaseCoordinator, dict[str, object]]:
        coord = _coordinator_from_tariff_entity(entity_id)
        if coord is None:
            _raise_service_validation(
                "tariff_rate_entity_invalid",
                placeholders={"entity_id": entity_id},
                message=f"Entity is not an Enphase tariff rate entity: {entity_id}",
            )
        assert coord is not None
        state = hass.states.get(entity_id)
        locator = None if state is None else state.attributes.get("tariff_locator")
        if not isinstance(locator, dict):
            _raise_service_validation(
                "tariff_rate_target_invalid",
                message="Tariff rate target is invalid.",
            )
        assert isinstance(locator, dict)
        if branch is not None and locator.get("branch") != branch:
            _raise_service_validation(
                "tariff_rate_entity_invalid",
                placeholders={"entity_id": entity_id},
                message=f"Entity is not an Enphase tariff rate entity: {entity_id}",
            )
        return coord, {"locator": locator, "rate": rate}

    def _format_tariff_rate(value: object) -> str:
        try:
            rate = float(str(value))
        except (TypeError, ValueError):
            _raise_service_validation(
                "tariff_rate_invalid",
                message="Tariff rate must be a non-negative number.",
            )
        if rate < 0:
            _raise_service_validation(
                "tariff_rate_invalid",
                message="Tariff rate must be a non-negative number.",
            )
        return f"{rate:.10f}".rstrip("0").rstrip(".") or "0"

    def _tariff_month(value: object, default: int) -> int:
        if value in (None, ""):
            return default
        try:
            month = int(float(str(value).strip()))
        except (TypeError, ValueError):
            _raise_service_validation(
                "tariff_structure_invalid",
                message="Tariff structure is invalid.",
            )
        if month < 1 or month > 12:
            _raise_service_validation(
                "tariff_structure_invalid",
                message="Tariff structure is invalid.",
            )
        return month

    def _tariff_minutes(value: object) -> int | str:
        if value in (None, ""):
            return ""
        if isinstance(value, str) and ":" in value:
            parts = value.strip().split(":", 1)
            try:
                hour = int(parts[0])
                minute = int(parts[1])
            except (TypeError, ValueError):
                _raise_service_validation(
                    "tariff_structure_invalid",
                    message="Tariff structure is invalid.",
                )
            minutes = hour * 60 + minute
        else:
            try:
                minutes = int(float(str(value).strip()))
            except (TypeError, ValueError):
                _raise_service_validation(
                    "tariff_structure_invalid",
                    message="Tariff structure is invalid.",
                )
        if minutes < 0 or minutes > 24 * 60:
            _raise_service_validation(
                "tariff_structure_invalid",
                message="Tariff structure is invalid.",
            )
        return minutes

    def _tariff_days(value: object, day_group_id: str) -> list[int]:
        if value in (None, ""):
            if day_group_id == "weekday":
                return [1, 2, 3, 4, 5]
            if day_group_id == "weekend":
                return [6, 7]
            return [1, 2, 3, 4, 5, 6, 7]
        raw_days = value if isinstance(value, list) else cv.ensure_list(value)
        days: list[int] = []
        for raw_day in raw_days:
            try:
                day = int(float(str(raw_day).strip()))
            except (TypeError, ValueError):
                _raise_service_validation(
                    "tariff_structure_invalid",
                    message="Tariff structure is invalid.",
                )
            if day < 1 or day > 7:
                _raise_service_validation(
                    "tariff_structure_invalid",
                    message="Tariff structure is invalid.",
                )
            days.append(day)
        return sorted(dict.fromkeys(days))

    def _season_key(row: dict[str, object]) -> tuple[str, int, int]:
        return (
            str(row.get("season_id") or row.get("season") or "default"),
            _tariff_month(row.get("start_month"), 1),
            _tariff_month(row.get("end_month"), 12),
        )

    def _guided_tou_seasons(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        if not rows:
            _raise_service_validation(
                "tariff_structure_invalid",
                message="Tariff structure is invalid.",
            )
        seasons: dict[tuple[str, int, int], dict[str, object]] = {}
        day_groups: dict[tuple[str, int, int], dict[str, dict[str, object]]] = {}
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                _raise_service_validation(
                    "tariff_structure_invalid",
                    message="Tariff structure is invalid.",
                )
            season_key = _season_key(row)
            season = seasons.get(season_key)
            if season is None:
                season = {
                    "id": season_key[0],
                    "startMonth": str(season_key[1]),
                    "endMonth": str(season_key[2]),
                    "days": [],
                }
                seasons[season_key] = season
            groups = day_groups.setdefault(season_key, {})
            day_group_id = str(
                row.get("day_group_id") or row.get("day_group") or "week"
            )
            day_group = groups.get(day_group_id)
            if day_group is None:
                day_group = {
                    "id": day_group_id,
                    "days": _tariff_days(row.get("days"), day_group_id),
                    "periods": [],
                    "updatedValue": "",
                }
                groups[day_group_id] = day_group
                days_value = season["days"]
                assert isinstance(days_value, list)
                days_value.append(day_group)
            period_id = str(row.get("period_id") or row.get("id") or f"period-{index}")
            periods_value = day_group["periods"]
            assert isinstance(periods_value, list)
            periods_value.append(
                {
                    "id": period_id,
                    "type": str(
                        row.get("period_type") or row.get("type") or "off-peak"
                    ),
                    "rate": _format_tariff_rate(row.get("rate")),
                    "startTime": _tariff_minutes(row.get("start_time")),
                    "endTime": _tariff_minutes(row.get("end_time")),
                    "rateComponents": [],
                }
            )
        return list(seasons.values())

    def _guided_tiered_seasons(
        rows: list[dict[str, object]], off_peak_rate: object | None
    ) -> list[dict[str, object]]:
        if not rows:
            _raise_service_validation(
                "tariff_structure_invalid",
                message="Tariff structure is invalid.",
            )
        seasons: dict[tuple[str, int, int], dict[str, object]] = {}
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                _raise_service_validation(
                    "tariff_structure_invalid",
                    message="Tariff structure is invalid.",
                )
            season_key = _season_key(row)
            season = seasons.get(season_key)
            if season is None:
                season = {
                    "id": season_key[0],
                    "startMonth": str(season_key[1]),
                    "endMonth": str(season_key[2]),
                    "offPeak": _format_tariff_rate(
                        row.get(
                            "off_peak_rate",
                            off_peak_rate if off_peak_rate is not None else 0,
                        )
                    ),
                    "tiers": [],
                }
                seasons[season_key] = season
            end_value = row.get("end_value", row.get("endValue", -1))
            tiers_value = season["tiers"]
            assert isinstance(tiers_value, list)
            tiers_value.append(
                {
                    "id": str(row.get("tier_id") or row.get("id") or f"tier-{index}"),
                    "rate": _format_tariff_rate(row.get("rate")),
                    "startValue": str(row.get("start_value", row.get("startValue", 0))),
                    "endValue": (
                        -1
                        if end_value in (None, "", -1, "-1")
                        else str(row.get("end_value", row.get("endValue")))
                    ),
                }
            )
        return list(seasons.values())

    def _guided_tariff_branch(
        data: dict[str, object],
        *,
        prefix: str,
        export: bool = False,
    ) -> dict[str, object] | None:
        if not data.get(f"configure_{prefix}_tariff"):
            return None
        tariff_type = data.get(f"{prefix}_tariff_type")
        if tariff_type is None:
            _raise_service_validation(
                "tariff_structure_invalid",
                message="Tariff structure is invalid.",
            )
        variation = str(data.get(f"{prefix}_variation", "single"))
        branch: dict[str, object] = {
            "typeKind": variation,
            "typeId": str(tariff_type),
            "source": "manual",
        }
        if export:
            branch["exportPlan"] = str(data.get("export_plan", "netFit"))
        if tariff_type == "tiered":
            raw_tiers = data.get(f"{prefix}_tiers")
            tier_rows = raw_tiers if isinstance(raw_tiers, list) else []
            branch["seasons"] = _guided_tiered_seasons(
                cast(list[dict[str, object]], tier_rows),
                data.get(f"{prefix}_off_peak_rate"),
            )
        else:
            raw_periods = data.get(f"{prefix}_periods")
            periods = cast(
                list[dict[str, object]],
                raw_periods if isinstance(raw_periods, list) else [],
            )
            if not periods and tariff_type == "flat":
                flat_rate = data.get(f"{prefix}_flat_rate")
                if flat_rate is None:
                    _raise_service_validation(
                        "tariff_structure_invalid",
                        message="Tariff structure is invalid.",
                    )
                periods = [
                    {
                        "season_id": "default",
                        "day_group_id": "week",
                        "days": [1, 2, 3, 4, 5, 6, 7],
                        "period_id": "off-peak",
                        "period_type": "off-peak",
                        "rate": flat_rate,
                        "start_time": "",
                        "end_time": "",
                    }
                ]
            branch["seasons"] = _guided_tou_seasons(periods)
        return branch

    async def _resolve_charger_targets(
        call: ServiceCall,
    ) -> list[tuple[str, str, EnphaseCoordinator]]:
        device_ids = _extract_device_ids(call)
        if not device_ids:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="grid_site_required",
            )

        targets: list[tuple[str, str, EnphaseCoordinator]] = []
        for device_id in device_ids:
            routing_context = await _resolve_device_routing_context(device_id)
            if routing_context is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="grid_site_required",
                )
            sn, site_id, config_entry_ids = routing_context
            coord = await _get_coordinator_for_sn(
                sn,
                site_id=site_id,
                config_entry_ids=config_entry_ids,
            )
            if coord is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="grid_site_required",
                )
            targets.append((device_id, sn, coord))

        return targets

    async def _svc_force_refresh(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        await coord.async_request_refresh()

    def _require_grid_profile_installer(runtime: object) -> None:
        if getattr(runtime, "installer_access_confirmed", False):
            return
        if getattr(runtime, "support_state", None) != SUPPORT_DENIED:
            _raise_service_validation(
                "grid_profile_unavailable",
                message="Grid profile control is unavailable.",
            )
        _raise_service_validation(
            "grid_profile_installer_required",
            message=(
                "Grid profile control requires installer-level Enphase "
                "Activation access."
            ),
        )

    async def _svc_browse_grid_profiles(call: ServiceCall) -> dict[str, object]:
        coord = await _resolve_single_site_coordinator(call)
        runtime = cast(GridProfileRuntime, coord.grid_profile_runtime)
        await runtime.async_refresh(force=False, load_profiles=False)
        _require_grid_profile_installer(runtime)
        await runtime.async_load_profiles(
            region_code=call.data.get("region_code"),
            commonly_used=call.data.get("commonly_used", True),
            force=False,
        )
        _require_grid_profile_installer(runtime)
        return runtime.browse_dict(
            region_code=call.data.get("region_code"),
            query=call.data.get("query"),
            commonly_used=call.data.get("commonly_used", True),
        )

    async def _svc_refresh_grid_profiles(call: ServiceCall) -> dict[str, object]:
        coord = await _resolve_single_site_coordinator(call)
        runtime = cast(GridProfileRuntime, coord.grid_profile_runtime)
        await runtime.async_refresh(force=True, load_profiles=False)
        _require_grid_profile_installer(runtime)
        await runtime.async_load_profiles(
            region_code=call.data.get("region_code"),
            commonly_used=call.data.get("commonly_used", True),
            force=True,
        )
        _require_grid_profile_installer(runtime)
        return runtime.browse_dict(
            region_code=call.data.get("region_code"),
            commonly_used=call.data.get("commonly_used", True),
        )

    async def _svc_set_grid_profile(call: ServiceCall) -> dict[str, object]:
        if not call.data.get("confirm"):
            _raise_service_validation(
                "grid_profile_confirmation_required",
                message="Confirmation is required to apply a grid profile.",
            )
        coord = await _resolve_single_site_coordinator(call)
        runtime = cast(GridProfileRuntime, coord.grid_profile_runtime)
        await runtime.async_refresh(force=False, load_profiles=False)
        _require_grid_profile_installer(runtime)
        region_code = call.data.get("region_code")
        if region_code:
            await runtime.async_load_profiles(
                region_code=region_code,
                commonly_used=True,
                force=False,
            )
            if runtime.profile_for_id(call.data["profile_id"]) is None:
                await runtime.async_load_profiles(
                    region_code=region_code,
                    commonly_used=False,
                    force=False,
                )
        return await runtime.async_apply_grid_profile(
            call.data["profile_id"],
            region_code=region_code,
            gateway_serial=call.data.get("gateway_serial"),
        )

    async def _svc_start(call: ServiceCall) -> None:
        connector_id = int(call.data.get("connector_id", 1))
        for _device_id, sn, coord in await _resolve_charger_targets(call):
            level = call.data.get("charging_level")
            await coord.async_start_charging(
                sn, requested_amps=level, connector_id=connector_id
            )

    async def _svc_stop(call: ServiceCall) -> None:
        for _device_id, sn, coord in await _resolve_charger_targets(call):
            await coord.async_stop_charging(sn)

    async def _svc_trigger(call: ServiceCall) -> dict[str, Any]:
        message = _service_trigger_message(call.data["requested_message"])
        _confirm_trigger_message(message, call.data.get("confirm_advanced", False))
        results: list[dict[str, object]] = []
        for device_id, sn, coord in await _resolve_charger_targets(call):
            reply = await coord.async_trigger_ocpp_message(sn, message)
            results.append(
                {
                    "device_id": device_id,
                    "serial": sn,
                    "site_id": coord.site_id,
                    "response": reply,
                }
            )
        return {"results": results}

    async def _svc_request_grid_otp(call: ServiceCall) -> None:
        coord = _get_coordinator_for_entry_id(call.data["config_entry_id"])
        if coord is None:
            _raise_service_validation(
                "grid_control_unavailable",
                message="Grid control is unavailable for this config entry.",
            )
        await coord.async_request_grid_toggle_otp()
        await coord.async_request_refresh()

    async def _svc_set_grid_mode(call: ServiceCall) -> None:
        coord = _get_coordinator_for_entry_id(call.data["config_entry_id"])
        if coord is None:
            _raise_service_validation(
                "grid_control_unavailable",
                message="Grid control is unavailable for this config entry.",
            )
        await coord.async_set_grid_mode(call.data["mode"], call.data["otp"])

    async def _svc_clear_issue(call: ServiceCall) -> None:
        site_ids: set[str] = set()
        for device_id in _extract_device_ids(call):
            site_id = await _resolve_site_id(device_id)
            if site_id:
                site_ids.add(site_id)
        explicit = call.data.get("site_id")
        if explicit:
            site_ids.add(str(explicit))

        issue_ids = {
            ISSUE_REAUTH_REQUIRED,
            ISSUE_AUTH_BLOCKED,
            ISSUE_TOO_MANY_ACTIVE_SESSIONS,
        }
        for site_id in site_ids:
            issue_ids.add(f"{ISSUE_REAUTH_REQUIRED}_{site_id}")
            issue_ids.add(f"{ISSUE_AUTH_BLOCKED}_{site_id}")
            issue_ids.add(f"{ISSUE_TOO_MANY_ACTIVE_SESSIONS}_{site_id}")
        for issue_id in issue_ids:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    async def _svc_clear_hems_auth_backoff(call: ServiceCall) -> dict[str, Any]:
        coord = await _resolve_single_site_coordinator(call)
        return await coord.async_clear_hems_auth_backoff()

    async def _svc_try_reauth_now(call: ServiceCall) -> dict[str, Any]:
        coord = await _resolve_single_site_coordinator(call)
        site_id = str(getattr(coord, "site_id", ""))
        has_stored_credentials = bool(
            getattr(coord, "_email", None)
            and getattr(coord, "_remember_password", False)
            and getattr(coord, "_stored_password", None)
        )
        if not has_stored_credentials:
            return {
                "site_id": site_id,
                "success": False,
                "reason": "stored_credentials_unavailable",
            }

        success = await coord.async_try_reauth_now()
        response = {
            "site_id": site_id,
            "success": bool(success.success),
            "reason": success.reason,
        }
        if success.retry_after_seconds is not None:
            response["retry_after_seconds"] = success.retry_after_seconds
        if success.success and bool(getattr(success, "performed_refresh", True)):
            await coord.async_request_refresh()
        return response

    async def _svc_start_stream(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        await coord.async_start_streaming(manual=True)
        await coord.async_request_refresh()

    async def _svc_stop_stream(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        await coord.async_stop_streaming(manual=True)
        await coord.async_request_refresh()

    async def _svc_update_cfg_schedule(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        await coord.async_update_cfg_schedule(
            start=call.data.get("start_time"),
            end=call.data.get("end_time"),
            limit=call.data.get("limit"),
        )

    async def _svc_add_schedule(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        if not coord.battery_write_access_confirmed:
            _raise_service_validation(
                "battery_schedule_editing_unavailable",
                message="Battery schedule editing is unavailable.",
            )
        schedule_type = str(call.data["schedule_type"]).lower()
        days = sorted({int(day) for day in call.data["days"]})
        limit = int(call.data["limit"])
        start_str, end_str = _validate_schedule_fields(
            schedule_type=schedule_type,
            start_time=call.data["start_time"],
            end_time=call.data["end_time"],
            days=days,
            limit=limit,
        )
        _validate_schedule_overlap(
            coord,
            start_time=start_str,
            end_time=end_str,
            days=days,
        )
        await _validate_schedule_with_api(coord, schedule_type)
        creator = getattr(coord.client, "create_battery_schedule", None)
        if not callable(creator):
            _raise_service_validation(
                "battery_schedule_api_unavailable",
                message="Battery schedule API is unavailable.",
            )
        try:
            await coord.battery_runtime.async_create_battery_schedule(
                schedule_type=str(schedule_type).upper(),
                start_time=start_str,
                end_time=end_str,
                limit=limit,
                days=days,
                timezone=str(
                    getattr(coord, "battery_timezone", None)
                    or hass.config.time_zone
                    or "UTC"
                ),
                is_enabled=True,
            )
        except aiohttp.ClientResponseError as err:
            coord.battery_runtime.raise_schedule_update_validation_error(err)
            raise
        await coord.async_request_refresh()

    async def _svc_update_schedule(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        if not coord.battery_write_access_confirmed:
            _raise_service_validation(
                "battery_schedule_editing_unavailable",
                message="Battery schedule editing is unavailable.",
            )
        if not call.data.get("confirm"):
            _raise_service_validation(
                "battery_schedule_update_confirm_required",
                message="Confirmation required to update a schedule.",
            )
        schedule_id = str(call.data["schedule_id"]).strip()
        if not SCHEDULE_ID_PATTERN.match(schedule_id):
            _raise_service_validation(
                "battery_schedule_id_invalid",
                placeholders={"schedule_id": schedule_id},
                message=f"Invalid schedule ID: {schedule_id}",
            )
        schedule_inventory = _schedule_inventory_by_id(coord)
        known_ids = set(schedule_inventory)
        if known_ids and schedule_id not in known_ids:
            _raise_service_validation(
                "battery_schedule_id_not_found",
                placeholders={"schedule_id": schedule_id},
                message=f"Schedule ID not found in current data: {schedule_id}",
            )
        existing_schedule = schedule_inventory.get(schedule_id)
        schedule_type = (
            existing_schedule.schedule_type
            if existing_schedule is not None
            else str(call.data["schedule_type"]).lower()
        )
        days = sorted({int(day) for day in call.data["days"]})
        limit = int(call.data["limit"])
        start_str, end_str = _validate_schedule_fields(
            schedule_type=schedule_type,
            start_time=call.data["start_time"],
            end_time=call.data["end_time"],
            days=days,
            limit=limit,
        )
        _validate_schedule_overlap(
            coord,
            start_time=start_str,
            end_time=end_str,
            days=days,
            exclude_schedule_id=schedule_id,
        )
        await _validate_schedule_with_api(coord, schedule_type)
        updater = getattr(coord.client, "update_battery_schedule", None)
        if not callable(updater):
            _raise_service_validation(
                "battery_schedule_api_unavailable",
                message="Battery schedule API is unavailable.",
            )
        apply_start_str, apply_end_str, apply_enabled = _apply_schedule_for_update(
            coord,
            schedule_inventory=schedule_inventory,
            schedule_id=schedule_id,
            schedule_type=schedule_type,
            start_time=start_str,
            end_time=end_str,
            enabled=(
                existing_schedule.enabled if existing_schedule is not None else None
            ),
        )
        try:
            await coord.battery_runtime.async_update_battery_schedule(
                schedule_id,
                schedule_type=str(schedule_type).upper(),
                start_time=start_str,
                end_time=end_str,
                limit=limit,
                days=days,
                timezone=str(
                    (
                        existing_schedule.timezone
                        if existing_schedule is not None
                        else getattr(coord, "battery_timezone", None)
                    )
                    or hass.config.time_zone
                    or "UTC"
                ),
                apply_settings=False,
            )
            if schedule_type == "cfg":
                await coord.battery_runtime.async_commit_cfg_schedule_write(
                    schedule_enabled=apply_enabled
                )
            else:
                await coord.battery_runtime.async_apply_schedule_family_settings(
                    schedule_type,
                    start_time=apply_start_str,
                    end_time=apply_end_str,
                    enabled=apply_enabled,
                )
        except aiohttp.ClientResponseError as err:
            coord.battery_runtime.raise_schedule_update_validation_error(err)
            raise
        await coord.async_request_refresh()

    async def _svc_delete_schedule(call: ServiceCall) -> None:
        coord = await _resolve_single_site_coordinator(call)
        if not coord.battery_write_access_confirmed:
            _raise_service_validation(
                "battery_schedule_editing_unavailable",
                message="Battery schedule editing is unavailable.",
            )
        if not call.data.get("confirm"):
            _raise_service_validation(
                "battery_schedule_delete_confirm_required",
                message="Confirmation required to delete a schedule.",
            )
        raw_schedule_ids = call.data.get("schedule_ids")
        if raw_schedule_ids:
            schedule_ids = _normalize_schedule_ids(raw_schedule_ids)
        else:
            schedule_ids = _normalize_schedule_ids(call.data.get("schedule_id"))
        if not schedule_ids:
            _raise_service_validation(
                "battery_schedule_ids_required",
                message="Provide at least one schedule ID to delete.",
            )
        invalid_ids = [
            schedule_id
            for schedule_id in schedule_ids
            if not SCHEDULE_ID_PATTERN.match(schedule_id)
        ]
        if invalid_ids:
            ids = ", ".join(invalid_ids)
            _raise_service_validation(
                "battery_schedule_ids_invalid",
                placeholders={"schedule_ids": ids},
                message=f"Invalid schedule ID(s): {ids}",
            )
        known_ids = _known_schedule_ids(coord)
        if known_ids:
            missing = [
                schedule_id
                for schedule_id in schedule_ids
                if schedule_id not in known_ids
            ]
            if missing:
                ids = ", ".join(missing)
                _raise_service_validation(
                    "battery_schedule_ids_not_found",
                    placeholders={"schedule_ids": ids},
                    message=f"Schedule ID(s) not found in current data: {ids}",
                )
        deleter = getattr(coord.client, "delete_battery_schedule", None)
        if not callable(deleter):
            _raise_service_validation(
                "battery_schedule_api_unavailable",
                message="Battery schedule API is unavailable.",
            )
        delete_schedule = cast(Callable[..., Awaitable[object]], deleter)
        schedule_inventory = _schedule_inventory_by_id(coord)
        requested_schedule_type = call.data.get("schedule_type")
        deleted_schedule_ids_by_family: dict[str, set[str]] = {}
        for schedule_id in schedule_ids:
            schedule = schedule_inventory.get(schedule_id)
            schedule_type = (
                schedule.schedule_type
                if schedule is not None
                else (
                    str(requested_schedule_type).lower()
                    if requested_schedule_type is not None
                    else "cfg"
                )
            )
            try:
                await delete_schedule(schedule_id, schedule_type=schedule_type)
            except aiohttp.ClientResponseError as err:
                coord.battery_runtime.raise_schedule_update_validation_error(err)
                raise
            deleted_schedule_ids_by_family.setdefault(
                str(schedule_type).lower(), set()
            ).add(schedule_id)
        for schedule_type, deleted_ids in deleted_schedule_ids_by_family.items():
            remaining_schedule = _remaining_schedule_for_delete_family(
                coord, schedule_type, deleted_ids
            )
            await coord.battery_runtime.async_apply_schedule_family_settings(
                schedule_type,
                start_time=(
                    remaining_schedule.start_time
                    if remaining_schedule is not None
                    else None
                ),
                end_time=(
                    remaining_schedule.end_time
                    if remaining_schedule is not None
                    else None
                ),
                enabled=(
                    remaining_schedule.enabled
                    if remaining_schedule is not None
                    else False
                ),
            )
        await coord.async_request_refresh()

    async def _svc_validate_schedule(call: ServiceCall) -> dict[str, Any]:
        coord = await _resolve_single_site_coordinator(call)
        if not coord.battery_write_access_confirmed:
            _raise_service_validation(
                "battery_schedule_editing_unavailable",
                message="Battery schedule editing is unavailable.",
            )
        result = await _validate_schedule_with_api(
            coord, str(call.data["schedule_type"]).lower()
        )
        return result

    async def _svc_sync_schedules(call: ServiceCall) -> None:
        for _device_id, sn, coord in await _resolve_charger_targets(call):
            await coord.schedule_sync.async_refresh(reason="service", serials=[sn])

    async def _svc_update_tariff(call: ServiceCall) -> None:
        rates = list(call.data.get("rates") or [])
        if "rate" in call.data:
            entity_ids = list(_extract_entity_ids(call))
            rate_entity = call.data.get("rate_entity")
            if rate_entity:
                entity_ids.append(str(rate_entity))
            entity_ids = list(dict.fromkeys(entity_ids))
            if len(entity_ids) != 1:
                _raise_service_validation(
                    "tariff_rate_entity_required",
                    message="Select exactly one tariff rate entity.",
                )
            rates.append({"entity_id": entity_ids[0], "rate": call.data["rate"]})
        if "import_rate" in call.data:
            rates.append(
                {
                    "entity_id": call.data["import_rate_entity"],
                    "rate": call.data["import_rate"],
                    "branch": "purchase",
                }
            )
        if "export_rate" in call.data:
            rates.append(
                {
                    "entity_id": call.data["export_rate_entity"],
                    "rate": call.data["export_rate"],
                    "branch": "buyback",
                }
            )
        billing_requested = bool(TARIFF_BILLING_FIELDS.intersection(call.data))
        guided_purchase_tariff = _guided_tariff_branch(call.data, prefix="import")
        guided_buyback_tariff = _guided_tariff_branch(
            call.data, prefix="export", export=True
        )
        structure_requested = bool(
            TARIFF_STRUCTURE_FIELDS.intersection(call.data)
            or guided_purchase_tariff is not None
            or guided_buyback_tariff is not None
        )
        if not rates and not billing_requested and not structure_requested:
            _raise_service_validation(
                "tariff_update_required",
                message="Provide billing details or at least one tariff rate update.",
            )
        if billing_requested and not TARIFF_BILLING_FIELDS.issubset(call.data):
            _raise_service_validation(
                "tariff_billing_incomplete",
                message="Provide billing start date, frequency, and interval.",
            )

        rate_updates: list[dict[str, object]] = []
        rate_coord: EnphaseCoordinator | None = None
        seen_entities: set[str] = set()
        for item in rates:
            entity_id = str(item.get("entity_id", "")).strip()
            if entity_id in seen_entities:
                _raise_service_validation(
                    "tariff_rate_entity_duplicate",
                    placeholders={"entity_id": entity_id},
                    message=f"Duplicate tariff rate entity: {entity_id}",
                )
            seen_entities.add(entity_id)
            coord, update = _tariff_rate_update_from_entity(
                entity_id,
                float(item["rate"]),
                branch=item.get("branch"),
            )
            if rate_coord is None:
                rate_coord = coord
            elif coord is not rate_coord:
                _raise_service_validation(
                    "tariff_site_mismatch",
                    message="All tariff updates must target the same Enphase site.",
                )
            rate_updates.append(update)

        billing: dict[str, object] | None = None
        if billing_requested:
            billing = {
                "billing_start_date": str(call.data["billing_start_date"]),
                "billing_frequency": str(call.data["billing_frequency"]),
                "billing_interval_value": int(call.data["billing_interval_value"]),
            }
        tariff_payload = call.data.get("tariff_payload")
        purchase_tariff = call.data.get("purchase_tariff") or guided_purchase_tariff
        buyback_tariff = call.data.get("buyback_tariff") or guided_buyback_tariff

        target_coord: EnphaseCoordinator
        has_explicit_site_target = bool(
            call.data.get("config_entry_id")
            or call.data.get("site_id")
            or _extract_device_ids(call)
        )
        if (billing_requested or structure_requested) and (
            has_explicit_site_target or rate_coord is None
        ):
            target_coord = await _resolve_single_site_coordinator(call)
            if rate_coord is not None and str(target_coord.site_id) != str(
                rate_coord.site_id
            ):
                _raise_service_validation(
                    "tariff_site_mismatch",
                    message="All tariff updates must target the same Enphase site.",
                )
        else:
            assert rate_coord is not None
            target_coord = rate_coord

        kwargs: dict[str, object] = {"billing": billing, "rate_updates": rate_updates}
        if tariff_payload is not None:
            kwargs["tariff_payload"] = tariff_payload
        if purchase_tariff is not None:
            kwargs["purchase_tariff"] = purchase_tariff
        if buyback_tariff is not None:
            kwargs["buyback_tariff"] = buyback_tariff
        await target_coord.tariff_runtime.async_update_tariff(**kwargs)

    hass.services.async_register(
        DOMAIN, "force_refresh", _svc_force_refresh, schema=FORCE_REFRESH_SCHEMA
    )
    grid_profile_browse_kwargs: dict[str, object] = {
        "schema": BROWSE_GRID_PROFILES_SCHEMA,
        "supports_response": supports_response.OPTIONAL,
    }
    hass.services.async_register(
        DOMAIN,
        "browse_grid_profiles",
        _svc_browse_grid_profiles,
        **grid_profile_browse_kwargs,
    )
    grid_profile_refresh_kwargs: dict[str, object] = {
        "schema": REFRESH_GRID_PROFILES_SCHEMA,
        "supports_response": supports_response.OPTIONAL,
    }
    hass.services.async_register(
        DOMAIN,
        "refresh_grid_profiles",
        _svc_refresh_grid_profiles,
        **grid_profile_refresh_kwargs,
    )
    grid_profile_set_kwargs: dict[str, object] = {
        "schema": SET_GRID_PROFILE_SCHEMA,
        "supports_response": supports_response.OPTIONAL,
    }
    hass.services.async_register(
        DOMAIN,
        "set_grid_profile",
        _svc_set_grid_profile,
        **grid_profile_set_kwargs,
    )
    hass.services.async_register(
        DOMAIN, "start_charging", _svc_start, schema=START_SCHEMA
    )
    hass.services.async_register(DOMAIN, "stop_charging", _svc_stop, schema=STOP_SCHEMA)

    hass.services.async_register(
        DOMAIN,
        "trigger_message",
        _svc_trigger,
        schema=TRIGGER_SCHEMA,
        supports_response=supports_response.OPTIONAL,
    )

    async_register_admin_service(
        hass,
        DOMAIN,
        "request_grid_toggle_otp",
        _svc_request_grid_otp,
        REQUEST_GRID_OTP_SCHEMA,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        "set_grid_mode",
        _svc_set_grid_mode,
        SET_GRID_MODE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "clear_reauth_issue", _svc_clear_issue, schema=CLEAR_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        "try_reauth_now",
        _svc_try_reauth_now,
        schema=CLEAR_SCHEMA,
        supports_response=supports_response.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "clear_hems_auth_backoff",
        _svc_clear_hems_auth_backoff,
        schema=CLEAR_SCHEMA,
        supports_response=supports_response.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, "start_live_stream", _svc_start_stream)
    hass.services.async_register(DOMAIN, "stop_live_stream", _svc_stop_stream)
    hass.services.async_register(
        DOMAIN, "sync_schedules", _svc_sync_schedules, schema=SYNC_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "add_schedule", _svc_add_schedule, schema=ADD_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "update_schedule", _svc_update_schedule, schema=UPDATE_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, "delete_schedule", _svc_delete_schedule, schema=DELETE_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        "validate_schedule",
        _svc_validate_schedule,
        schema=VALIDATE_SCHEDULE_SCHEMA,
        supports_response=supports_response.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "update_cfg_schedule",
        _svc_update_cfg_schedule,
        schema=UPDATE_CFG_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        "update_tariff",
        _svc_update_tariff,
        schema=UPDATE_TARIFF_SCHEMA,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove registered integration services."""

    for service in REGISTERED_SERVICES:
        hass.services.async_remove(DOMAIN, service)
