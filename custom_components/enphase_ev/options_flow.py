"""Home Assistant options and advanced operations for Enphase entries."""

from __future__ import annotations
from typing import Any
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult, section
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.selector import selector
from homeassistant.helpers.translation import (
    async_get_cached_translations,
    async_get_translations,
)
from .api import (
    AuthTokens,
    async_fetch_devices_inventory,
    async_fetch_battery_site_settings,
    async_fetch_chargers,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_COOKIE,
    CONF_EAUTH,
    CONF_INCLUDE_INVERTERS,
    CONF_REMEMBER_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SELECTED_TYPE_KEYS,
    CONF_SERIALS,
    CONF_SESSION_ID,
    CONF_SITE_ID,
    CONF_SITE_ONLY,
    CONF_TOKEN_EXPIRES_AT,
    DEFAULT_BATTERY_SCHEDULES_ENABLED,
    DEFAULT_API_TIMEOUT,
    DEFAULT_DEGRADED_SERVICE_REPAIR_ISSUES,
    DEFAULT_FAST_POLL_INTERVAL,
    DEFAULT_GRID_PROFILE_CONTROLS_ENABLED,
    DEFAULT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
    DEFAULT_MICROINVERTER_POWER_ENABLED,
    DEFAULT_PRICING_EDITS_ENABLED,
    DEFAULT_SCHEDULE_SYNC_ENABLED,
    DEFAULT_SYSTEM_EVENT_REPAIR_ISSUES,
    DEFAULT_SLOW_POLL_INTERVAL,
    DEFAULT_WEATHER_ENABLED,
    DEFAULT_VPP_EVENTS_ENABLED,
    DOMAIN,
    MAX_API_TIMEOUT,
    MAX_POLL_INTERVAL,
    MAX_SESSION_HISTORY_INTERVAL_MIN,
    MIN_API_TIMEOUT,
    MIN_FAST_POLL_INTERVAL,
    MIN_SESSION_HISTORY_INTERVAL_MIN,
    MIN_SLOW_POLL_INTERVAL,
    OPT_API_TIMEOUT,
    OPT_BATTERY_SCHEDULES_ENABLED,
    OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
    OPT_FAST_POLL_INTERVAL,
    OPT_FAST_WHILE_STREAMING,
    OPT_GRID_PROFILE_CONTROLS_ENABLED,
    OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
    OPT_MICROINVERTER_POWER_ENABLED,
    OPT_PRICING_EDITS_ENABLED,
    OPT_NOMINAL_VOLTAGE,
    OPT_SLOW_POLL_INTERVAL,
    OPT_SESSION_HISTORY_INTERVAL,
    OPT_WEATHER_ENABLED,
    OPT_VPP_EVENTS_ENABLED,
    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
    OPT_SCHEDULE_SYNC_ENABLED,
    OPT_SYSTEM_EVENT_REPAIR_ISSUES,
)
from .device_types import (
    ONBOARDING_SUPPORTED_TYPE_KEYS,
    active_type_serials_from_inventory,
)
from .envoy_history import (
    EnvoyHistoryCandidate,
    EnvoyHistorySource,
    EnvoyHistoryTarget,
    candidate_options,
    discover_enphase_targets,
    discover_external_migration_candidates,
    discover_envoy_sources,
    execute_takeover,
    format_completed_preview,
    format_mapping_preview,
    format_selection_preview,
    format_warning_preview,
    migration_flow_fields,
    selection_uses_source,
    selected_mappings,
    skip_option_value,
    source_by_entry_id,
    source_options,
    suggest_mappings,
    validate_selected_mappings,
)
from .grid_profile_runtime import (
    ALL_PROFILES_OPTION,
    COMMONLY_USED_OPTION,
    SUPPORT_DENIED,
    SUPPORT_READ_ONLY,
    GridProfile,
    GridProfileRuntime,
)
from .config_selection import normalize_serials, normalize_selected_type_keys
from .log_redaction import redact_site_id, redact_text
from .runtime_data import EnphaseConfigEntry
from .runtime_helpers import normalize_poll_intervals
from .voltage import coerce_nominal_voltage, resolve_nominal_voltage_for_hass

from .config_flow_support import (
    CONFIG_ENTRY_MINOR_VERSION,
    _LOGGER as _LOGGER,
    MFA_RESEND_DELAY_SECONDS as MFA_RESEND_DELAY_SECONDS,
    CONF_OTP as CONF_OTP,
    CONF_RESEND_CODE as CONF_RESEND_CODE,
    CONF_TYPE_ENVOY as CONF_TYPE_ENVOY,
    CONF_TYPE_ENCHARGE as CONF_TYPE_ENCHARGE,
    CONF_TYPE_AC_BATTERY as CONF_TYPE_AC_BATTERY,
    CONF_TYPE_IQEVSE as CONF_TYPE_IQEVSE,
    CONF_TYPE_HEATPUMP as CONF_TYPE_HEATPUMP,
    CONF_TYPE_MICROINVERTER as CONF_TYPE_MICROINVERTER,
    CONF_DEVICE_CATEGORIES_SECTION as CONF_DEVICE_CATEGORIES_SECTION,
    _load_get_clientsession as _load_get_clientsession,
    async_get_clientsession as async_get_clientsession,
    CONF_DEVICE_FEATURES_SECTION as CONF_DEVICE_FEATURES_SECTION,
    CONF_MIGRATION_SOURCE_ENTRY as CONF_MIGRATION_SOURCE_ENTRY,
    CONF_MIGRATION_BACKUP_CONFIRMED as CONF_MIGRATION_BACKUP_CONFIRMED,
    CONF_MIGRATION_CONFIRM_REASSIGN as CONF_MIGRATION_CONFIRM_REASSIGN,
    CONF_MIGRATION_DISABLE_ARCHIVED as CONF_MIGRATION_DISABLE_ARCHIVED,
    CONF_GRID_PROFILE_REGION as CONF_GRID_PROFILE_REGION,
    CONF_GRID_PROFILE_COMMONLY_USED as CONF_GRID_PROFILE_COMMONLY_USED,
    CONF_GRID_PROFILE_ID as CONF_GRID_PROFILE_ID,
    CONF_GRID_PROFILE_CONFIRM_APPLY as CONF_GRID_PROFILE_CONFIRM_APPLY,
    CONF_GRID_MODE as CONF_GRID_MODE,
    CONF_GRID_MODE_CONFIRM as CONF_GRID_MODE_CONFIRM,
    _GRID_PROFILE_LABEL_PREFIX as _GRID_PROFILE_LABEL_PREFIX,
    _GRID_MODE_LABEL_PREFIX as _GRID_MODE_LABEL_PREFIX,
    _GRID_CONTROL_BLOCK_REASON_LABEL_PREFIX as _GRID_CONTROL_BLOCK_REASON_LABEL_PREFIX,
    _TYPE_FIELD_BY_KEY as _TYPE_FIELD_BY_KEY,
    _battery_site_settings_has_acb as _battery_site_settings_has_acb,
    _site_entry_title as _site_entry_title,
    _coerce_int_value as _coerce_int_value,
    _bounded_int as _bounded_int,
    _clamped_int as _clamped_int,
    _hems_heatpump_available as _hems_heatpump_available,
    _legacy_microinverters_available as _legacy_microinverters_available,
)


class OptionsFlowHandler(config_entries.OptionsFlow):  # type: ignore[misc]
    def __init__(self, config_entry: EnphaseConfigEntry) -> None:
        super().__init__()
        self._entry = config_entry
        self._migration_sources: list[EnvoyHistorySource] | None = None
        self._migration_targets: dict[str, EnvoyHistoryTarget] | None = None
        self._migration_extra_candidates: list[EnvoyHistoryCandidate] | None = None
        self._selected_migration_source_id: str | None = None
        self._migration_selection: dict[str, str] = {}
        self._grid_profile_apply_result: dict[str, object] | None = None
        self._grid_mode_target: str | None = None

    @staticmethod
    def _normalize_serials(value: Any) -> list[str]:
        return normalize_serials(value)

    @staticmethod
    def _normalize_type_keys(value: Any) -> list[str]:
        return normalize_selected_type_keys(value, allowed=_TYPE_FIELD_BY_KEY)

    @staticmethod
    def _normalize_any_type_keys(value: Any) -> list[str]:
        return normalize_selected_type_keys(value)

    def _legacy_selected_type_keys(
        self,
        serials: list[str],
        include_inverters: bool,
        *,
        site_only: bool = False,
    ) -> list[str]:
        selected = ["envoy", "encharge"]
        if serials and not site_only:
            selected.append("iqevse")
        if include_inverters:
            selected.append("microinverter")
        return selected

    def _stored_selected_type_keys(self) -> list[str]:
        if CONF_SELECTED_TYPE_KEYS in self._entry.data:
            return self._normalize_any_type_keys(
                self._entry.data.get(CONF_SELECTED_TYPE_KEYS, [])
            )
        return self._legacy_selected_type_keys(
            self._normalize_serials(self._entry.data.get(CONF_SERIALS, [])),
            bool(self._entry.data.get(CONF_INCLUDE_INVERTERS, True)),
            site_only=bool(self._entry.data.get(CONF_SITE_ONLY, False)),
        )

    def _default_selected_type_keys(self) -> list[str]:
        selected = set(self._stored_selected_type_keys())
        return [key for key in ONBOARDING_SUPPORTED_TYPE_KEYS if key in selected]

    def _default_nominal_voltage(self) -> int:
        configured: int | None = coerce_nominal_voltage(
            self._entry.options.get(OPT_NOMINAL_VOLTAGE)
        )
        if configured is not None:
            return configured

        runtime_data = getattr(self._entry, "runtime_data", None)
        coordinator = getattr(runtime_data, "coordinator", None)
        if coordinator is not None:
            preferred = getattr(coordinator, "preferred_nominal_voltage", None)
            if callable(preferred):
                value: int | None = coerce_nominal_voltage(preferred())
                if value is not None:
                    return value
            nominal: int | None = coerce_nominal_voltage(
                getattr(coordinator, "nominal_voltage", None)
            )
            if nominal is not None:
                return nominal

        return int(resolve_nominal_voltage_for_hass(self.hass))

    def _entry_auth_tokens(self) -> AuthTokens | None:
        site_id = str(self._entry.data.get(CONF_SITE_ID, "") or "").strip()
        access_token = self._entry.data.get(CONF_EAUTH) or self._entry.data.get(
            CONF_ACCESS_TOKEN
        )
        cookie = self._entry.data.get(CONF_COOKIE)
        if not site_id or not access_token or not cookie:
            return None
        return AuthTokens(
            cookie=str(cookie),
            session_id=self._entry.data.get(CONF_SESSION_ID),
            access_token=access_token,
            token_expires_at=self._entry.data.get(CONF_TOKEN_EXPIRES_AT),
        )

    async def _ac_battery_supported_for_options(self) -> bool:
        selected = set(self._stored_selected_type_keys())
        if "ac_battery" in selected:
            return True
        tokens = self._entry_auth_tokens()
        site_id = str(self._entry.data.get(CONF_SITE_ID, "") or "").strip()
        if tokens is None or not site_id:
            return False
        payload = await async_fetch_battery_site_settings(
            await async_get_clientsession(self.hass),
            site_id,
            tokens,
        )
        return _battery_site_settings_has_acb(payload)

    async def _settings_type_keys(self) -> list[str]:
        visible: list[str] = []
        ac_battery_supported = await self._ac_battery_supported_for_options()
        for type_key in ONBOARDING_SUPPORTED_TYPE_KEYS:
            if type_key == "ac_battery" and not ac_battery_supported:
                continue
            if type_key in _TYPE_FIELD_BY_KEY:
                visible.append(type_key)
        return visible

    def _build_devices_schema(self, visible_type_keys: list[str]) -> vol.Schema:
        default_selected_type_keys = self._default_selected_type_keys()
        category_fields: dict[vol.Marker, object] = {}
        for type_key in visible_type_keys:
            field_key = _TYPE_FIELD_BY_KEY.get(type_key)
            if field_key is None:
                continue
            category_fields[
                vol.Optional(field_key, default=type_key in default_selected_type_keys)
            ] = bool
        feature_fields: dict[vol.Marker, object] = {
            vol.Optional(
                OPT_SCHEDULE_SYNC_ENABLED,
                default=self._entry.options.get(
                    OPT_SCHEDULE_SYNC_ENABLED,
                    DEFAULT_SCHEDULE_SYNC_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_BATTERY_SCHEDULES_ENABLED,
                default=self._entry.options.get(
                    OPT_BATTERY_SCHEDULES_ENABLED,
                    DEFAULT_BATTERY_SCHEDULES_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_PRICING_EDITS_ENABLED,
                default=self._entry.options.get(
                    OPT_PRICING_EDITS_ENABLED,
                    DEFAULT_PRICING_EDITS_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_WEATHER_ENABLED,
                default=self._entry.options.get(
                    OPT_WEATHER_ENABLED,
                    DEFAULT_WEATHER_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_VPP_EVENTS_ENABLED,
                default=self._entry.options.get(
                    OPT_VPP_EVENTS_ENABLED,
                    DEFAULT_VPP_EVENTS_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_GRID_PROFILE_CONTROLS_ENABLED,
                default=self._entry.options.get(
                    OPT_GRID_PROFILE_CONTROLS_ENABLED,
                    DEFAULT_GRID_PROFILE_CONTROLS_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
                default=self._entry.options.get(
                    OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
                    DEFAULT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_MICROINVERTER_POWER_ENABLED,
                default=self._entry.options.get(
                    OPT_MICROINVERTER_POWER_ENABLED,
                    DEFAULT_MICROINVERTER_POWER_ENABLED,
                ),
            ): bool,
            vol.Optional(
                OPT_NOMINAL_VOLTAGE,
                default=self._default_nominal_voltage(),
            ): int,
        }
        return vol.Schema(
            {
                vol.Required(CONF_DEVICE_CATEGORIES_SECTION): section(
                    vol.Schema(category_fields)
                ),
                vol.Required(CONF_DEVICE_FEATURES_SECTION): section(
                    vol.Schema(feature_fields)
                ),
            }
        )

    @staticmethod
    def _build_authentication_schema() -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional("reauth", default=False): bool,
                vol.Optional("forget_password", default=False): bool,
            }
        )

    def _build_repair_notifications_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional(
                    OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
                    default=self._entry.options.get(
                        OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
                        DEFAULT_DEGRADED_SERVICE_REPAIR_ISSUES,
                    ),
                ): bool,
                vol.Optional(
                    OPT_SYSTEM_EVENT_REPAIR_ISSUES,
                    default=self._entry.options.get(
                        OPT_SYSTEM_EVENT_REPAIR_ISSUES,
                        DEFAULT_SYSTEM_EVENT_REPAIR_ISSUES,
                    ),
                ): bool,
            }
        )

    def _build_general_settings_schema(self) -> vol.Schema:
        fast_default, slow_default = normalize_poll_intervals(
            self._entry.options.get(OPT_FAST_POLL_INTERVAL, DEFAULT_FAST_POLL_INTERVAL),
            self._entry.options.get(OPT_SLOW_POLL_INTERVAL, DEFAULT_SLOW_POLL_INTERVAL),
        )
        schema_fields: dict[vol.Marker, object] = {
            vol.Optional(
                OPT_FAST_POLL_INTERVAL,
                default=fast_default,
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_FAST_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
            ),
            vol.Optional(
                OPT_SLOW_POLL_INTERVAL,
                default=slow_default,
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_SLOW_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
            ),
            vol.Optional(
                OPT_FAST_WHILE_STREAMING,
                default=self._entry.options.get(OPT_FAST_WHILE_STREAMING, True),
            ): bool,
            vol.Optional(
                OPT_API_TIMEOUT,
                default=self._entry.options.get(OPT_API_TIMEOUT, DEFAULT_API_TIMEOUT),
            ): selector(
                {
                    "number": {
                        "min": MIN_API_TIMEOUT,
                        "max": MAX_API_TIMEOUT,
                        "step": 1,
                        "mode": "box",
                        "unit_of_measurement": "s",
                    }
                }
            ),
            vol.Optional(
                OPT_SESSION_HISTORY_INTERVAL,
                default=self._entry.options.get(
                    OPT_SESSION_HISTORY_INTERVAL,
                    DEFAULT_SESSION_HISTORY_INTERVAL_MIN,
                ),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(
                    min=MIN_SESSION_HISTORY_INTERVAL_MIN,
                    max=MAX_SESSION_HISTORY_INTERVAL_MIN,
                ),
            ),
        }
        base_schema = vol.Schema(schema_fields)
        return self.add_suggested_values_to_schema(base_schema, self._entry.options)

    def _build_settings_schema(self) -> vol.Schema:
        return self._build_general_settings_schema()

    def _build_schema(self) -> vol.Schema:
        """Backward-compatible alias for tests and legacy direct calls."""

        return self._build_general_settings_schema()

    async def _load_migration_sources(self) -> list[EnvoyHistorySource]:
        if self._migration_sources is None:
            self._migration_sources = await discover_envoy_sources(self.hass)
        return self._migration_sources

    def _load_migration_targets(self) -> dict[str, EnvoyHistoryTarget]:
        if self._migration_targets is None:
            self._migration_targets = discover_enphase_targets(self.hass, self._entry)
        return self._migration_targets

    async def _load_migration_extra_candidates(self) -> list[EnvoyHistoryCandidate]:
        if self._migration_extra_candidates is None:
            self._migration_extra_candidates = (
                await discover_external_migration_candidates(self.hass, self._entry)
            )
        return self._migration_extra_candidates

    async def _selected_migration_source(self) -> EnvoyHistorySource | None:
        return source_by_entry_id(
            await self._load_migration_sources(), self._selected_migration_source_id
        )

    async def _async_reload_migration_source_entry(
        self,
        source: EnvoyHistorySource,
        source_entry: (
            config_entries.ConfigEntry | None
        ),  # quality-scale: external-config-entry
    ) -> bool:
        if source_entry is None:
            return True
        try:
            reloaded = await self.hass.config_entries.async_reload(source.entry_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed reloading Envoy source entry after migration: %s",
                redact_text(err),
            )
            return False
        if reloaded:
            object.__setattr__(
                source_entry,
                "state",
                config_entries.ConfigEntryState.LOADED,
            )
        return bool(reloaded)

    def _migration_flow_keys(self) -> tuple[str, ...]:
        targets = self._load_migration_targets()
        return tuple(
            flow_key for flow_key in migration_flow_fields() if flow_key in targets
        )

    def _build_migration_source_schema(
        self, sources: list[EnvoyHistorySource]
    ) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_MIGRATION_SOURCE_ENTRY): selector(
                    {
                        "select": {
                            "options": source_options(sources),
                            "mode": "dropdown",
                        }
                    }
                )
            }
        )

    def _build_migration_intro_schema(self) -> vol.Schema:
        return vol.Schema(
            {vol.Required(CONF_MIGRATION_BACKUP_CONFIRMED, default=False): bool}
        )

    def _build_migration_mapping_schema(
        self,
        source: EnvoyHistorySource,
        extra_candidates: list[EnvoyHistoryCandidate],
        defaults: dict[str, str] | None = None,
    ) -> vol.Schema:
        defaults = defaults or {}
        field_schema: dict[vol.Marker, object] = {}
        selector_config = {
            "select": {
                "options": candidate_options(source, extra_candidates),
                "mode": "dropdown",
            }
        }
        for flow_key in self._migration_flow_keys():
            default_value = defaults.get(flow_key)
            marker = (
                vol.Optional(flow_key)
                if default_value is None
                else vol.Optional(flow_key, default=default_value)
            )
            field_schema[marker] = selector(selector_config)
        return vol.Schema(field_schema)

    def _build_migration_confirm_schema(
        self, *, disable_archived_default: bool
    ) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_MIGRATION_CONFIRM_REASSIGN, default=False): bool,
                vol.Required(
                    CONF_MIGRATION_DISABLE_ARCHIVED,
                    default=disable_archived_default,
                ): bool,
            }
        )

    async def _discover_iqevse_serials(self) -> list[str]:
        site_id = str(self._entry.data.get(CONF_SITE_ID, "")).strip()
        if not site_id:
            return []

        tokens = AuthTokens(
            cookie=str(self._entry.data.get(CONF_COOKIE, "") or ""),
            session_id=self._entry.data.get(CONF_SESSION_ID),
            access_token=self._entry.data.get(CONF_EAUTH)
            or self._entry.data.get(CONF_ACCESS_TOKEN),
            token_expires_at=self._entry.data.get(CONF_TOKEN_EXPIRES_AT),
        )
        session = await async_get_clientsession(self.hass)

        discovered: list[str] = []
        try:
            payload = await async_fetch_devices_inventory(session, site_id, tokens)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to fetch device inventory during IQ EVSE serial discovery "
                "for site %s: %s",
                redact_site_id(site_id),
                redact_text(err, site_ids=(site_id,)),
            )
        else:
            discovered = self._normalize_serials(
                active_type_serials_from_inventory(payload, type_key="iqevse")
            )
        if discovered:
            return discovered

        chargers = await async_fetch_chargers(session, site_id, tokens)
        for charger in chargers:
            if charger.serial:
                serial = str(charger.serial).strip()
                if serial and serial not in discovered:
                    discovered.append(serial)
        return discovered

    def _grid_profile_runtime(self) -> GridProfileRuntime | None:
        runtime_data = getattr(self._entry, "runtime_data", None)
        coordinator = getattr(runtime_data, "coordinator", None)
        runtime = getattr(coordinator, "grid_profile_runtime", None)
        return runtime if isinstance(runtime, GridProfileRuntime) else None

    def _grid_mode_coordinator(self) -> Any | None:
        runtime_data = getattr(self._entry, "runtime_data", None)
        return getattr(runtime_data, "coordinator", None)

    async def _async_prime_grid_mode_labels(self) -> None:
        language = getattr(self.hass.config, "language", "en")
        await async_get_translations(self.hass, language, "selector", [DOMAIN])

    def _grid_mode_label(self, mode: object) -> str:
        key = str(mode or "unknown").strip().lower() or "unknown"
        language = getattr(self.hass.config, "language", "en")
        path = f"{_GRID_MODE_LABEL_PREFIX}{key}"
        for candidate_language in (language, "en"):
            translated = async_get_cached_translations(
                self.hass,
                candidate_language,
                "selector",
                DOMAIN,
            ).get(path)
            if isinstance(translated, str) and translated.strip():
                return translated
        return key.replace("_", " ").title()

    def _grid_control_block_reason_label(self, reason: object) -> str:
        key = str(reason or "unknown").strip().lower() or "unknown"
        language = getattr(self.hass.config, "language", "en")
        for reason_key in (key, "unknown"):
            path = f"{_GRID_CONTROL_BLOCK_REASON_LABEL_PREFIX}{reason_key}"
            for candidate_language in (language, "en"):
                translated = async_get_cached_translations(
                    self.hass,
                    candidate_language,
                    "selector",
                    DOMAIN,
                ).get(path)
                if isinstance(translated, str) and translated.strip():
                    return translated
        return "Unknown blocking condition"

    def _grid_mode_placeholders(self) -> dict[str, str]:
        coordinator = self._grid_mode_coordinator()
        current_mode = getattr(coordinator, "grid_mode", None)
        return {
            "current_mode": self._grid_mode_label(current_mode),
            "target_mode": self._grid_mode_label(self._grid_mode_target),
        }

    @staticmethod
    def _grid_mode_error(err: ServiceValidationError) -> str:
        return str(getattr(err, "translation_key", None) or "grid_control_unavailable")

    def _grid_profile_options_available(self) -> bool:
        runtime = self._grid_profile_runtime()
        return bool(
            self._grid_profile_controls_enabled()
            and runtime is not None
            and runtime.installer_access_confirmed
            and runtime.regions
        )

    def _grid_profile_controls_enabled(self) -> bool:
        """Return the option state, preserving pre-migration options-flow behavior."""

        if OPT_GRID_PROFILE_CONTROLS_ENABLED in self._entry.options:
            return bool(self._entry.options[OPT_GRID_PROFILE_CONTROLS_ENABLED])
        return bool(self._entry.minor_version < CONFIG_ENTRY_MINOR_VERSION)

    @staticmethod
    def _grid_profile_unavailable_reason(runtime: GridProfileRuntime) -> str:
        return (
            "grid_profile_installer_required"
            if runtime.support_state in {SUPPORT_DENIED, SUPPORT_READ_ONLY}
            else "grid_profile_unavailable"
        )

    async def _async_grid_profile_runtime_for_options(
        self,
    ) -> GridProfileRuntime | None:
        if not self._grid_profile_controls_enabled():
            return None
        runtime = self._grid_profile_runtime()
        if runtime is None:
            return None
        if runtime.installer_access_confirmed and runtime.regions:
            return runtime
        await runtime.async_refresh(force=False, load_profiles=False)
        return runtime

    def _grid_profile_region_options(
        self, runtime: GridProfileRuntime
    ) -> list[dict[str, str]]:
        return [
            {"value": region.region_code, "label": region.label}
            for region in runtime.regions
        ]

    def _grid_profile_options(
        self, profiles: list[GridProfile]
    ) -> list[dict[str, str]]:
        return [
            {"value": profile.profile_id, "label": profile.option_label}
            for profile in profiles
        ]

    @staticmethod
    def _grid_profile_commonly_used_from_input(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return bool(value != ALL_PROFILES_OPTION)

    def _grid_profile_filter_schema(self, runtime: GridProfileRuntime) -> vol.Schema:
        selected_region = runtime.staged_region_code
        if selected_region is None and runtime.regions:
            selected_region = runtime.regions[0].region_code
        return vol.Schema(
            {
                vol.Required(
                    CONF_GRID_PROFILE_REGION,
                    default=selected_region,
                ): selector(
                    {
                        "select": {
                            "options": self._grid_profile_region_options(runtime),
                            "mode": "dropdown",
                        }
                    }
                ),
                vol.Optional(
                    CONF_GRID_PROFILE_COMMONLY_USED,
                    default=runtime.list_mode_option,
                ): selector(
                    {
                        "select": {
                            "options": [COMMONLY_USED_OPTION, ALL_PROFILES_OPTION],
                            "mode": "dropdown",
                            "translation_key": "grid_profile_list_mode",
                        }
                    }
                ),
            }
        )

    def _grid_profile_select_schema(self, profiles: list[GridProfile]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_GRID_PROFILE_ID): selector(
                    {
                        "select": {
                            "options": self._grid_profile_options(profiles),
                            "mode": "dropdown",
                        }
                    }
                )
            }
        )

    def _grid_profile_confirm_schema(self, runtime: GridProfileRuntime) -> vol.Schema:
        if not runtime.apply_available:
            return vol.Schema({})
        return vol.Schema(
            {vol.Required(CONF_GRID_PROFILE_CONFIRM_APPLY, default=False): bool}
        )

    async def _async_prime_grid_profile_labels(self) -> None:
        language = getattr(self.hass.config, "language", "en")
        await async_get_translations(
            self.hass,
            language,
            "selector",
            [DOMAIN],
        )

    def _grid_profile_status_label(self, key: str) -> str:
        language = getattr(self.hass.config, "language", "en")
        path = f"{_GRID_PROFILE_LABEL_PREFIX}{key}"
        for candidate_language in (language, "en"):
            translated = async_get_cached_translations(
                self.hass,
                candidate_language,
                "selector",
                DOMAIN,
            ).get(path)
            if isinstance(translated, str) and translated.strip():
                return translated
        return key.replace("_", " ").capitalize()

    def _grid_profile_flag_label(self, value: bool | None) -> str:
        if value is True:
            return self._grid_profile_status_label("yes")
        if value is False:
            return self._grid_profile_status_label("no")
        return self._grid_profile_status_label("unknown")

    def _grid_profile_confirm_placeholders(
        self, runtime: GridProfileRuntime
    ) -> dict[str, str]:
        profile = runtime.profile_for_id_in_region(
            runtime.staged_profile_id,
            runtime.staged_region_code,
        )
        unknown = self._grid_profile_status_label("unknown")
        return {
            "country": runtime.country_code or unknown,
            "region": runtime.staged_region_label
            or runtime.staged_region_code
            or unknown,
            "current_profile": runtime.current_profile_display() or unknown,
            "selected_profile": profile.option_label if profile else unknown,
            "selected_profile_id": profile.profile_id if profile else unknown,
            "selected_profile_pel": self._grid_profile_flag_label(
                profile.pel_enabled if profile else None
            ),
            "selected_profile_277v": self._grid_profile_flag_label(
                profile.is_277v_compatible if profile else None
            ),
            "apply_status": self._grid_profile_status_label(
                "available" if runtime.apply_available else "unavailable"
            ),
        }

    def _grid_profile_applied_placeholders(
        self, runtime: GridProfileRuntime | None
    ) -> dict[str, str]:
        placeholders: dict[str, str] = {}
        if runtime is not None:
            placeholders.update(self._grid_profile_confirm_placeholders(runtime))

        requested_profile_id = placeholders.get(
            "selected_profile_id", self._grid_profile_status_label("unknown")
        )
        result = self._grid_profile_apply_result
        cloud_apply_status = self._grid_profile_status_label("accepted")
        if isinstance(result, dict):
            requested = result.get("requested_profile_id")
            if requested:
                requested_profile_id = str(requested)
            status = result.get("cloud_apply_status")
            if status:
                cloud_apply_status = self._grid_profile_status_label(str(status))

        placeholders["requested_profile_id"] = requested_profile_id
        placeholders["cloud_apply_status"] = cloud_apply_status
        return placeholders

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            if "reauth" in user_input or "forget_password" in user_input:
                return await self.async_step_authentication_settings(user_input)
            return await self.async_step_settings(user_input)
        menu_options = [
            "settings",
            "devices",
            "repair_notifications",
            "authentication_settings",
            "advanced",
            "migrate_envoy",
        ]
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        del user_input
        menu_options = ["grid_toggle"]
        if self._grid_profile_options_available():
            menu_options.append("grid_profile")
        return self.async_show_menu(
            step_id="advanced",
            menu_options=menu_options,
        )

    async def async_step_grid_toggle(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        coordinator = self._grid_mode_coordinator()
        if coordinator is None:
            return self.async_abort(reason="grid_mode_unavailable")

        errors: dict[str, str] = {}
        await self._async_prime_grid_mode_labels()
        if user_input is None:
            try:
                refreshed = (
                    await coordinator.battery_runtime.async_refresh_grid_control_check(
                        force=True
                    )
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Failed refreshing Grid Mode eligibility for site %s: %s",
                    redact_site_id(getattr(coordinator, "site_id", "")),
                    redact_text(
                        err,
                        site_ids=(str(getattr(coordinator, "site_id", "")),),
                    ),
                )
                return self.async_abort(reason="grid_mode_unavailable")

            if refreshed is not True:
                return self.async_abort(reason="grid_mode_unavailable")
            if getattr(coordinator, "grid_control_supported", None) is not True:
                return self.async_abort(reason="grid_mode_unavailable")
            if getattr(coordinator, "grid_toggle_allowed", None) is not True:
                reasons = getattr(coordinator, "grid_toggle_blocked_reasons", [])
                return self.async_abort(
                    reason="grid_mode_blocked",
                    description_placeholders={
                        "reasons": ", ".join(
                            self._grid_control_block_reason_label(reason)
                            for reason in (reasons or ["pending"])
                        )
                    },
                )
        else:
            target = str(user_input.get(CONF_GRID_MODE, "")).strip().lower()
            current = str(getattr(coordinator, "grid_mode", "") or "").lower()
            if target not in {"on_grid", "off_grid"}:
                errors[CONF_GRID_MODE] = "grid_mode_invalid"
            elif target == current:
                errors[CONF_GRID_MODE] = "grid_mode_already_active"
            else:
                self._grid_mode_target = target
                try:
                    await coordinator.async_request_grid_toggle_otp()
                except ServiceValidationError as err:
                    errors["base"] = self._grid_mode_error(err)
                else:
                    return await self.async_step_grid_toggle_otp()

        return self.async_show_form(
            step_id="grid_toggle",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_GRID_MODE): selector(
                        {
                            "select": {
                                "options": ["on_grid", "off_grid"],
                                "mode": "dropdown",
                                "translation_key": "grid_mode",
                            }
                        }
                    )
                }
            ),
            description_placeholders=self._grid_mode_placeholders(),
            errors=errors,
        )

    async def async_step_grid_toggle_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        coordinator = self._grid_mode_coordinator()
        if coordinator is None or self._grid_mode_target not in {
            "on_grid",
            "off_grid",
        }:
            return self.async_abort(reason="grid_mode_unavailable")

        errors: dict[str, str] = {}
        await self._async_prime_grid_mode_labels()
        if user_input is not None:
            if not user_input.get(CONF_GRID_MODE_CONFIRM):
                errors["base"] = "grid_mode_confirm_required"
            else:
                try:
                    await coordinator.async_set_grid_mode(
                        self._grid_mode_target,
                        user_input.get(CONF_OTP, ""),
                    )
                except ServiceValidationError as err:
                    errors["base"] = self._grid_mode_error(err)
                else:
                    return await self.async_step_grid_toggle_applied()

        return self.async_show_form(
            step_id="grid_toggle_otp",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_OTP): selector({"text": {"type": "password"}}),
                    vol.Required(CONF_GRID_MODE_CONFIRM, default=False): bool,
                }
            ),
            description_placeholders=self._grid_mode_placeholders(),
            errors=errors,
        )

    async def async_step_grid_toggle_applied(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        await self._async_prime_grid_mode_labels()
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(self._entry.options))
        return self.async_show_form(
            step_id="grid_toggle_applied",
            data_schema=vol.Schema({}),
            description_placeholders=self._grid_mode_placeholders(),
        )

    async def async_step_grid_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        runtime = await self._async_grid_profile_runtime_for_options()
        if runtime is None:
            return self.async_abort(reason="grid_profile_unavailable")
        if not runtime.installer_access_confirmed:
            return self.async_abort(
                reason=self._grid_profile_unavailable_reason(runtime)
            )
        if not runtime.regions:
            return self.async_abort(reason="grid_profile_no_regions")

        errors: dict[str, str] = {}
        if user_input is not None:
            region_code = str(user_input.get(CONF_GRID_PROFILE_REGION, "")).strip()
            commonly_used = self._grid_profile_commonly_used_from_input(
                user_input.get(CONF_GRID_PROFILE_COMMONLY_USED, COMMONLY_USED_OPTION)
            )
            if runtime.region_for_code(region_code) is None:
                errors[CONF_GRID_PROFILE_REGION] = "grid_profile_region_invalid"
            else:
                runtime.set_region(region_code)
                runtime.set_list_mode(
                    COMMONLY_USED_OPTION if commonly_used else ALL_PROFILES_OPTION
                )
                runtime.set_search_query(None)
                await runtime.async_load_profiles(
                    region_code=region_code,
                    commonly_used=commonly_used,
                    force=True,
                )
                if not runtime.installer_access_confirmed:
                    return self.async_abort(
                        reason=self._grid_profile_unavailable_reason(runtime)
                    )
                if not runtime.filtered_profiles():
                    errors["base"] = "grid_profile_no_profiles"
                else:
                    return await self.async_step_grid_profile_select()

        return self.async_show_form(
            step_id="grid_profile",
            data_schema=self._grid_profile_filter_schema(runtime),
            errors=errors,
        )

    async def async_step_grid_profile_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        runtime = self._grid_profile_runtime()
        if runtime is None:
            return self.async_abort(reason="grid_profile_unavailable")
        if not runtime.installer_access_confirmed:
            return self.async_abort(
                reason=self._grid_profile_unavailable_reason(runtime)
            )

        profiles = runtime.filtered_profiles()
        if not profiles:
            return await self.async_step_grid_profile()

        errors: dict[str, str] = {}
        if user_input is not None:
            profile_id = str(user_input.get(CONF_GRID_PROFILE_ID, "")).strip()
            if (
                runtime.profile_for_id_in_region(
                    profile_id,
                    runtime.staged_region_code,
                )
                is None
            ):
                errors[CONF_GRID_PROFILE_ID] = "grid_profile_profile_invalid"
            else:
                runtime.set_staged_profile(profile_id)
                return await self.async_step_grid_profile_confirm()

        return self.async_show_form(
            step_id="grid_profile_select",
            data_schema=self._grid_profile_select_schema(profiles),
            errors=errors,
        )

    async def async_step_grid_profile_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        runtime = self._grid_profile_runtime()
        if runtime is None:
            return self.async_abort(reason="grid_profile_unavailable")
        if not runtime.installer_access_confirmed:
            return self.async_abort(
                reason=self._grid_profile_unavailable_reason(runtime)
            )
        if (
            runtime.profile_for_id_in_region(
                runtime.staged_profile_id,
                runtime.staged_region_code,
            )
            is None
        ):
            return await self.async_step_grid_profile_select()

        errors: dict[str, str] = {}
        await self._async_prime_grid_profile_labels()
        if user_input is not None:
            if not runtime.apply_available:
                errors["base"] = "grid_profile_gateway_required"
            elif not user_input.get(CONF_GRID_PROFILE_CONFIRM_APPLY):
                errors["base"] = "confirm_required"
            else:
                try:
                    self._grid_profile_apply_result = await runtime.async_apply_staged()
                except ServiceValidationError as err:
                    errors["base"] = getattr(
                        err, "translation_key", "grid_profile_apply_failed"
                    )
                else:
                    return await self.async_step_grid_profile_applied()

        return self.async_show_form(
            step_id="grid_profile_confirm",
            data_schema=self._grid_profile_confirm_schema(runtime),
            description_placeholders=self._grid_profile_confirm_placeholders(runtime),
            errors=errors,
        )

    async def async_step_grid_profile_applied(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        runtime = self._grid_profile_runtime()
        await self._async_prime_grid_profile_labels()
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data=dict(self._entry.options),
            )
        placeholders: dict[str, str] = {}
        if runtime is not None:
            placeholders = self._grid_profile_applied_placeholders(runtime)
        return self.async_show_form(
            step_id="grid_profile_applied",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        schema = self._build_settings_schema()
        if user_input is not None:
            option_data = dict(user_input)
            option_data.pop("forget_password", None)
            option_data.pop("reauth", None)
            errors: dict[str, str] = {}
            if OPT_API_TIMEOUT in option_data:
                api_timeout = _bounded_int(
                    option_data[OPT_API_TIMEOUT],
                    minimum=MIN_API_TIMEOUT,
                    maximum=MAX_API_TIMEOUT,
                )
                if api_timeout is None:
                    errors[OPT_API_TIMEOUT] = "unknown"
                else:
                    option_data[OPT_API_TIMEOUT] = api_timeout
            if OPT_SESSION_HISTORY_INTERVAL in option_data:
                session_history_interval = _bounded_int(
                    option_data[OPT_SESSION_HISTORY_INTERVAL],
                    minimum=MIN_SESSION_HISTORY_INTERVAL_MIN,
                    maximum=MAX_SESSION_HISTORY_INTERVAL_MIN,
                )
                if session_history_interval is None:
                    errors[OPT_SESSION_HISTORY_INTERVAL] = "unknown"
                else:
                    option_data[OPT_SESSION_HISTORY_INTERVAL] = session_history_interval
            if errors:
                return self.async_show_form(
                    step_id="settings",
                    data_schema=self.add_suggested_values_to_schema(schema, user_input),
                    errors=errors,
                )
            fast_poll, slow_poll = normalize_poll_intervals(
                option_data.get(OPT_FAST_POLL_INTERVAL, DEFAULT_FAST_POLL_INTERVAL),
                option_data.get(OPT_SLOW_POLL_INTERVAL, DEFAULT_SLOW_POLL_INTERVAL),
            )
            option_data[OPT_FAST_POLL_INTERVAL] = fast_poll
            option_data[OPT_SLOW_POLL_INTERVAL] = slow_poll
            for field_key in _TYPE_FIELD_BY_KEY.values():
                option_data.pop(field_key, None)
            option_data.pop(OPT_SCHEDULE_SYNC_ENABLED, None)
            option_data.pop(OPT_BATTERY_SCHEDULES_ENABLED, None)
            option_data.pop(OPT_DEGRADED_SERVICE_REPAIR_ISSUES, None)
            option_data.pop(OPT_SYSTEM_EVENT_REPAIR_ISSUES, None)
            option_data.pop(OPT_PRICING_EDITS_ENABLED, None)
            option_data.pop(OPT_WEATHER_ENABLED, None)
            option_data.pop(OPT_VPP_EVENTS_ENABLED, None)
            option_data.pop(OPT_GRID_PROFILE_CONTROLS_ENABLED, None)
            option_data.pop(OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED, None)
            option_data.pop(OPT_MICROINVERTER_POWER_ENABLED, None)
            option_data.pop(OPT_NOMINAL_VOLTAGE, None)
            option_data.pop(CONF_SCAN_INTERVAL, None)
            option_data.pop(CONF_SITE_ONLY, None)

            options = dict(self._entry.options)
            options.update(option_data)
            return self.async_create_entry(data=options)

        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        visible_type_keys = await self._settings_type_keys()
        schema = self._build_devices_schema(visible_type_keys)
        if user_input is None:
            return self.async_show_form(step_id="devices", data_schema=schema)

        submitted_data = dict(user_input)
        category_data = submitted_data.pop(CONF_DEVICE_CATEGORIES_SECTION, None)
        feature_data = submitted_data.pop(CONF_DEVICE_FEATURES_SECTION, None)
        device_data = dict(category_data) if isinstance(category_data, dict) else {}
        if isinstance(feature_data, dict):
            device_data.update(feature_data)
        device_data.update(submitted_data)
        selected_type_keys: list[str] = []
        default_selected_type_keys = self._default_selected_type_keys()
        for type_key in visible_type_keys:
            field_key = _TYPE_FIELD_BY_KEY.get(type_key)
            if field_key is None:
                continue
            if bool(device_data.pop(field_key, type_key in default_selected_type_keys)):
                selected_type_keys.append(type_key)

        for type_key in self._stored_selected_type_keys():
            if (
                type_key not in ONBOARDING_SUPPORTED_TYPE_KEYS
                and type_key not in selected_type_keys
            ):
                selected_type_keys.append(type_key)

        serials = self._normalize_serials(self._entry.data.get(CONF_SERIALS, []))
        site_only = "iqevse" not in selected_type_keys
        if site_only:
            serials = []
        elif not serials:
            serials = await self._discover_iqevse_serials()
            if not serials:
                return self.async_show_form(
                    step_id="devices",
                    data_schema=self.add_suggested_values_to_schema(schema, user_input),
                    errors={"base": "serials_required"},
                )

        new_data = dict(self._entry.data)
        new_data[CONF_SELECTED_TYPE_KEYS] = selected_type_keys
        new_data[CONF_SITE_ONLY] = site_only
        new_data[CONF_INCLUDE_INVERTERS] = "microinverter" in selected_type_keys
        new_data[CONF_SERIALS] = serials
        options = dict(self._entry.options)
        options[OPT_SCHEDULE_SYNC_ENABLED] = bool(
            device_data.get(
                OPT_SCHEDULE_SYNC_ENABLED,
                options.get(
                    OPT_SCHEDULE_SYNC_ENABLED,
                    DEFAULT_SCHEDULE_SYNC_ENABLED,
                ),
            )
        )
        options[OPT_BATTERY_SCHEDULES_ENABLED] = bool(
            device_data.get(
                OPT_BATTERY_SCHEDULES_ENABLED,
                options.get(
                    OPT_BATTERY_SCHEDULES_ENABLED,
                    DEFAULT_BATTERY_SCHEDULES_ENABLED,
                ),
            )
        )
        options[OPT_PRICING_EDITS_ENABLED] = bool(
            device_data.get(
                OPT_PRICING_EDITS_ENABLED,
                options.get(
                    OPT_PRICING_EDITS_ENABLED,
                    DEFAULT_PRICING_EDITS_ENABLED,
                ),
            )
        )
        options[OPT_WEATHER_ENABLED] = bool(
            device_data.get(
                OPT_WEATHER_ENABLED,
                options.get(
                    OPT_WEATHER_ENABLED,
                    DEFAULT_WEATHER_ENABLED,
                ),
            )
        )
        options[OPT_VPP_EVENTS_ENABLED] = bool(
            device_data.get(
                OPT_VPP_EVENTS_ENABLED,
                options.get(
                    OPT_VPP_EVENTS_ENABLED,
                    DEFAULT_VPP_EVENTS_ENABLED,
                ),
            )
        )
        options[OPT_GRID_PROFILE_CONTROLS_ENABLED] = bool(
            device_data.get(
                OPT_GRID_PROFILE_CONTROLS_ENABLED,
                options.get(
                    OPT_GRID_PROFILE_CONTROLS_ENABLED,
                    DEFAULT_GRID_PROFILE_CONTROLS_ENABLED,
                ),
            )
        )
        options[OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED] = bool(
            device_data.get(
                OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
                options.get(
                    OPT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
                    DEFAULT_MICROINVERTER_LIFETIME_ENERGY_ENABLED,
                ),
            )
        )
        options[OPT_MICROINVERTER_POWER_ENABLED] = bool(
            device_data.get(
                OPT_MICROINVERTER_POWER_ENABLED,
                options.get(
                    OPT_MICROINVERTER_POWER_ENABLED,
                    DEFAULT_MICROINVERTER_POWER_ENABLED,
                ),
            )
        )
        options[OPT_NOMINAL_VOLTAGE] = (
            coerce_nominal_voltage(
                device_data.get(
                    OPT_NOMINAL_VOLTAGE,
                    options.get(OPT_NOMINAL_VOLTAGE, self._default_nominal_voltage()),
                )
            )
            or self._default_nominal_voltage()
        )
        self.hass.config_entries.async_update_entry(
            self._entry,
            data=new_data,
            options=options,
        )
        return self.async_create_entry(title="", data=options)

    async def async_step_repair_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        schema = self._build_repair_notifications_schema()
        if user_input is None:
            return self.async_show_form(
                step_id="repair_notifications",
                data_schema=schema,
            )

        options = dict(self._entry.options)
        options[OPT_DEGRADED_SERVICE_REPAIR_ISSUES] = bool(
            user_input.get(
                OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
                options.get(
                    OPT_DEGRADED_SERVICE_REPAIR_ISSUES,
                    DEFAULT_DEGRADED_SERVICE_REPAIR_ISSUES,
                ),
            )
        )
        options[OPT_SYSTEM_EVENT_REPAIR_ISSUES] = bool(
            user_input.get(
                OPT_SYSTEM_EVENT_REPAIR_ISSUES,
                options.get(
                    OPT_SYSTEM_EVENT_REPAIR_ISSUES,
                    DEFAULT_SYSTEM_EVENT_REPAIR_ISSUES,
                ),
            )
        )
        return self.async_create_entry(title="", data=options)

    async def async_step_authentication_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="authentication_settings",
                data_schema=self._build_authentication_schema(),
            )

        new_data = dict(self._entry.data)
        if bool(user_input.get("forget_password", False)):
            new_data.pop(CONF_PASSWORD, None)
            new_data[CONF_REMEMBER_PASSWORD] = False
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)
        if bool(user_input.get("reauth", False)):
            self._entry.async_start_reauth(self.hass, data=new_data)
        return self.async_create_entry(title="", data=dict(self._entry.options))

    async def async_step_migrate_envoy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        del user_input
        sources = await self._load_migration_sources()
        targets = self._load_migration_targets()
        if not sources:
            return self.async_abort(reason="migration_no_envoy_sources")
        if not targets:
            return self.async_abort(reason="migration_no_targets")
        if len(sources) == 1:
            self._selected_migration_source_id = sources[0].entry_id
            return await self.async_step_migrate_envoy_intro()
        return await self.async_step_migrate_envoy_source()

    async def async_step_migrate_envoy_source(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        sources = await self._load_migration_sources()
        if user_input is not None:
            self._selected_migration_source_id = user_input.get(
                CONF_MIGRATION_SOURCE_ENTRY
            )
            self._migration_selection = {}
            return await self.async_step_migrate_envoy_intro()

        return self.async_show_form(
            step_id="migrate_envoy_source",
            data_schema=self._build_migration_source_schema(sources),
        )

    async def async_step_migrate_envoy_intro(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        source = await self._selected_migration_source()
        if source is None:
            return await self.async_step_migrate_envoy()
        errors: dict[str, str] = {}
        if user_input is not None:
            if bool(user_input.get(CONF_MIGRATION_BACKUP_CONFIRMED)):
                return await self.async_step_migrate_envoy_mapping()
            errors["base"] = "backup_required"

        return self.async_show_form(
            step_id="migrate_envoy_intro",
            data_schema=self._build_migration_intro_schema(),
            errors=errors,
            description_placeholders={
                "source_title": source.title,
            },
        )

    async def async_step_migrate_envoy_mapping(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        source = await self._selected_migration_source()
        if source is None:
            return await self.async_step_migrate_envoy()
        extra_candidates = await self._load_migration_extra_candidates()

        defaults = dict(self._migration_selection)
        if not defaults:
            defaults.update(
                suggest_mappings(
                    source,
                    self._load_migration_targets(),
                    extra_candidates,
                )
            )

        errors: dict[str, str] = {}
        if user_input is not None:
            self._migration_selection = selected_mappings(user_input)
            defaults = {
                flow_key: str(user_input.get(flow_key, skip_option_value()))
                for flow_key in self._migration_flow_keys()
            }
            validation = validate_selected_mappings(
                self.hass,
                self._entry,
                source,
                self._load_migration_targets(),
                self._migration_selection,
                extra_candidates,
                require_source_unloaded=False,
            )
            if validation.error is None:
                return await self.async_step_migrate_envoy_confirm()
            errors["base"] = validation.error

        return self.async_show_form(
            step_id="migrate_envoy_mapping",
            data_schema=self._build_migration_mapping_schema(
                source,
                extra_candidates,
                defaults,
            ),
            errors=errors,
        )

    async def async_step_migrate_envoy_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        source = await self._selected_migration_source()
        if source is None:
            return await self.async_step_migrate_envoy()
        extra_candidates = await self._load_migration_extra_candidates()

        validation = validate_selected_mappings(
            self.hass,
            self._entry,
            source,
            self._load_migration_targets(),
            self._migration_selection,
            extra_candidates,
            require_source_unloaded=False,
        )
        if not self._migration_selection:
            return await self.async_step_migrate_envoy_mapping(
                {**self._migration_selection}
            )
        disable_archived_default = selection_uses_source(
            source,
            self._migration_selection,
            extra_candidates,
        )

        errors: dict[str, str] = {}
        if user_input is not None:
            if not bool(user_input.get(CONF_MIGRATION_CONFIRM_REASSIGN)):
                errors["base"] = "confirm_required"
            else:
                source_entry = self.hass.config_entries.async_get_entry(source.entry_id)
                disable_archived_entities = bool(
                    user_input.get(
                        CONF_MIGRATION_DISABLE_ARCHIVED, disable_archived_default
                    )
                )
                source_selected = selection_uses_source(
                    source,
                    self._migration_selection,
                    extra_candidates,
                )
                source_was_loaded = (
                    source_selected
                    and source_entry is not None
                    and source_entry.state is config_entries.ConfigEntryState.LOADED
                )
                if source_was_loaded:
                    unloaded = await self.hass.config_entries.async_unload(
                        source.entry_id
                    )
                    if not unloaded:
                        errors["base"] = "envoy_entry_loaded"
                    elif source_entry is not None:
                        object.__setattr__(
                            source_entry,
                            "state",
                            config_entries.ConfigEntryState.NOT_LOADED,
                        )

                validation = validate_selected_mappings(
                    self.hass,
                    self._entry,
                    source,
                    self._load_migration_targets(),
                    self._migration_selection,
                    extra_candidates,
                    require_source_unloaded=source_was_loaded,
                )
                if errors:
                    pass
                elif validation.error is not None:
                    if source_was_loaded:
                        await self._async_reload_migration_source_entry(
                            source, source_entry
                        )
                    errors["base"] = validation.error
                else:
                    execution_error = execute_takeover(
                        self.hass,
                        validation.mappings,
                        disable_archived_entities=disable_archived_entities,
                    )
                    if execution_error is not None:
                        if source_was_loaded:
                            await self._async_reload_migration_source_entry(
                                source, source_entry
                            )
                        return self.async_abort(
                            reason="migration_partial_failure",
                            description_placeholders={
                                "completed_mappings": format_completed_preview(
                                    execution_error.completed
                                ),
                                "failed_entity_id": (
                                    execution_error.failed.old_entity_id
                                    if execution_error.failed is not None
                                    else "unknown"
                                ),
                                "failure_reason": execution_error.reason,
                            },
                        )
                    reload_description = "migration_success"
                    try:
                        current_reloaded = await self.hass.config_entries.async_reload(
                            self._entry.entry_id
                        )
                        if not current_reloaded:
                            reload_description = "migration_success_reload_needed"
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug(
                            "Failed reloading config entry after Envoy migration: %s",
                            redact_text(err),
                        )
                        reload_description = "migration_success_reload_needed"
                    if source_was_loaded:
                        if not await self._async_reload_migration_source_entry(
                            source, source_entry
                        ):
                            reload_description = "migration_success_reload_needed"
                    return self.async_create_entry(
                        title="",
                        data=dict(self._entry.options),
                        description=reload_description,
                    )

        return self.async_show_form(
            step_id="migrate_envoy_confirm",
            data_schema=self._build_migration_confirm_schema(
                disable_archived_default=disable_archived_default
            ),
            errors=errors,
            description_placeholders={
                "mapping_preview": (
                    format_mapping_preview(validation.mappings)
                    if validation.error is None
                    else format_selection_preview(
                        self._migration_selection,
                        self._load_migration_targets(),
                    )
                ),
                "warning_preview": format_warning_preview(validation.warnings),
            },
        )
