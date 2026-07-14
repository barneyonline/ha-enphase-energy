"""Runtime support for cloud Activation grid profile controls."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from homeassistant.exceptions import ServiceValidationError

from .api import ActivationAccessDenied, Unauthorized
from .const import DOMAIN
from .service_validation import raise_translated_service_validation

ACTIVATION_GRID_PROFILE_FAMILY = "activation_grid_profile"
SUPPORT_UNKNOWN = "unknown"
SUPPORT_CONFIRMED = "installer_access_confirmed"
SUPPORT_DENIED = "installer_access_denied"
SUPPORT_UNAVAILABLE = "activation_unavailable"
COMMONLY_USED_OPTION = "commonly_used"
ALL_PROFILES_OPTION = "all_profiles"
PROFILE_MODE_OPTIONS = (COMMONLY_USED_OPTION, ALL_PROFILES_OPTION)
PENDING_PROFILE_POLL_INTERVAL_S = 60.0
PENDING_PROFILE_POLL_WINDOW_S = 300.0


@dataclass(slots=True, frozen=True)
class ActivationRegion:
    """Activation region/state reference."""

    country_code: str
    region_code: str
    region_name: str
    region_id: int | None = None

    @property
    def label(self) -> str:
        """Return a stable display label."""

        if self.region_name:
            return f"{self.region_code}, {self.country_code} - {self.region_name}"
        return f"{self.region_code}, {self.country_code}"


@dataclass(slots=True, frozen=True)
class GridProfile:
    """One cloud Activation grid profile."""

    profile_id: str
    name: str
    group: str
    country: str
    state: str
    pel_enabled: bool | None = None
    is_277v_compatible: bool | None = None
    recommended: bool = False

    @property
    def option_label(self) -> str:
        """Return label used by the staged profile select."""

        return f"{self.group}: {self.name}"


@dataclass(slots=True, frozen=True)
class GatewayGridProfileTarget:
    """Gateway metadata required by the Activation apply endpoint."""

    serial_num: str
    part_num: str | None
    ensemble_envoy: bool
    current_profile_id: str | None = None
    current_profile_name: str | None = None
    requested_profile_id: str | None = None
    requested_profile_name: str | None = None


@dataclass(slots=True)
class GridProfileBrowseResult:
    """Structured browse/search result."""

    support_state: str
    country_code: str | None
    regions: list[dict[str, object]] = field(default_factory=list)
    profiles: list[dict[str, object]] = field(default_factory=list)
    grouped_profiles: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    current_profile: dict[str, object] | None = None
    requested_profile: dict[str, object] | None = None
    staged: dict[str, object] = field(default_factory=dict)


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    return text or None


def _clean_upper(value: object) -> str | None:
    text = _clean_text(value)
    return text.upper() if text else None


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _clean_text(value)
    if text is None:
        return None
    if text.lower() in {"true", "1", "yes", "y"}:
        return True
    if text.lower() in {"false", "0", "no", "n"}:
        return False
    return None


def _first_present(record: dict[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in record:
            return record[key]
    return None


def _profile_id_for_compare(profile_id: str | None) -> str | None:
    text = _clean_text(profile_id)
    if text and text.lower().startswith("agf:"):
        return text[4:]
    return text


def _profile_dict(profile: GridProfile) -> dict[str, object]:
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "group": profile.group,
        "country": profile.country,
        "state": profile.state,
        "pel_enabled": profile.pel_enabled,
        "is_277v_compatible": profile.is_277v_compatible,
        "recommended": profile.recommended,
        "option_label": profile.option_label,
    }


def _region_dict(region: ActivationRegion) -> dict[str, object]:
    return {
        "country_code": region.country_code,
        "region_code": region.region_code,
        "region_name": region.region_name,
        "region_id": region.region_id,
        "label": region.label,
    }


def _group_matches_region(group_label: str, *, country: str, state: str) -> bool:
    """Return whether an Activation profile group belongs to the requested region."""

    parts = [part.strip().upper() for part in group_label.split(",")]
    if not parts or not parts[0]:
        return True
    if parts[0] != state:
        return False
    if len(parts) > 1 and parts[1] and parts[1] != country:
        return False
    return True


class GridProfileRuntime:
    """Own cloud Activation grid profile state for one coordinator."""

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        self.client = coordinator.client
        self.support_state = SUPPORT_UNKNOWN
        self.installer_access_ever_confirmed = False
        self.country_code: str | None = None
        self.site_region_code: str | None = None
        self.reference_payload: dict[str, object] | None = None
        self.regions_by_country: dict[str, list[ActivationRegion]] = {}
        self.catalog_cache: dict[
            tuple[str, str, bool], tuple[float, list[GridProfile]]
        ] = {}
        self.activation_record: dict[str, object] | None = None
        self.current_profile_id: str | None = None
        self.current_profile_name: str | None = None
        self.requested_profile_id: str | None = None
        self.requested_profile_name: str | None = None
        self.gateway_targets: dict[str, GatewayGridProfileTarget] = {}
        self.staged_region_code: str | None = None
        self.staged_commonly_used: bool = True
        self.staged_query: str = ""
        self.staged_profile_id: str | None = None
        self.pending_profile_id: str | None = None
        self.pending_gateway_serial: str | None = None
        self.pending_started_mono: float | None = None
        self._pending_refresh_task: asyncio.Task[None] | None = None
        self._pending_poll_interval_s = PENDING_PROFILE_POLL_INTERVAL_S
        self._pending_poll_window_s = PENDING_PROFILE_POLL_WINDOW_S
        self._lock = asyncio.Lock()
        self._apply_lock = asyncio.Lock()

    def _publish_state_update(self) -> None:
        """Publish a grid-profile transition through the coordinator boundary."""

        publish = getattr(self.coordinator, "publish_runtime_state_update", None)
        if callable(publish):
            publish("grid_profile")
            return
        # Compatibility for the lightweight coordinator used by isolated tests.
        self.coordinator.async_update_listeners()

    @property
    def installer_access_confirmed(self) -> bool:
        return self.support_state == SUPPORT_CONFIRMED

    @property
    def list_mode_option(self) -> str:
        return (
            COMMONLY_USED_OPTION if self.staged_commonly_used else ALL_PROFILES_OPTION
        )

    @property
    def regions(self) -> list[ActivationRegion]:
        if not self.country_code:
            return []
        return list(self.regions_by_country.get(self.country_code, ()))

    @property
    def region_options(self) -> list[str]:
        return [region.label for region in self.regions]

    @property
    def staged_region_label(self) -> str | None:
        region = self.region_for_code(self.staged_region_code)
        return region.label if region is not None else None

    @property
    def staged_profile_label(self) -> str | None:
        profile = self.profile_for_id(self.staged_profile_id)
        return profile.option_label if profile is not None else None

    @property
    def staged_profile_options(self) -> list[str]:
        return [profile.option_label for profile in self.filtered_profiles()]

    @property
    def apply_available(self) -> bool:
        return (
            self.installer_access_confirmed
            and self.staged_profile_id is not None
            and len(self.gateway_targets) == 1
        )

    @property
    def status(self) -> str:
        if self.pending_profile_id:
            return "pending"
        if self.installer_access_confirmed:
            return "available"
        return self.support_state

    def region_for_code(self, code: str | None) -> ActivationRegion | None:
        normalized = _clean_upper(code)
        if normalized is None:
            return None
        for region in self.regions:
            if region.region_code == normalized:
                return region
        return None

    def region_code_for_label(self, label: str) -> str | None:
        text = _clean_text(label)
        if not text:
            return None
        for region in self.regions:
            if region.label == text or region.region_code == text:
                return region.region_code
        return None

    def profile_for_id(self, profile_id: str | None) -> GridProfile | None:
        compare = _profile_id_for_compare(profile_id)
        if compare is None:
            return None
        keys: list[tuple[str, str, bool]] = []
        if self.country_code and self.staged_region_code:
            keys.append(
                (self.country_code, self.staged_region_code, self.staged_commonly_used)
            )
            keys.append(
                (
                    self.country_code,
                    self.staged_region_code,
                    not self.staged_commonly_used,
                )
            )
        for key in keys:
            cached = self.catalog_cache.get(key)
            if cached is None:
                continue
            for profile in cached[1]:
                if _profile_id_for_compare(profile.profile_id) == compare:
                    return profile
        for (country, _state, _mode), (
            _expires,
            profiles,
        ) in self.catalog_cache.items():
            if (country, _state, _mode) in keys:
                continue
            if self.country_code and country != self.country_code:
                continue
            for profile in profiles:
                if _profile_id_for_compare(profile.profile_id) == compare:
                    return profile
        return None

    def profile_for_id_in_region(
        self, profile_id: str | None, region_code: str | None
    ) -> GridProfile | None:
        """Return a profile only when cached for the requested site region."""

        compare = _profile_id_for_compare(profile_id)
        country = self.country_code
        state = _clean_upper(region_code)
        if compare is None or not country or not state:
            return None
        for mode in (True, False):
            cached = self.catalog_cache.get((country, state, mode))
            if cached is None:
                continue
            for profile in cached[1]:
                if _profile_id_for_compare(profile.profile_id) == compare:
                    return profile
        return None

    def current_profile_display(self) -> str | None:
        """Return the best available current grid profile display value."""

        details = self._current_profile_details()
        name = details.get("profile_name")
        if isinstance(name, str) and name:
            return name
        profile_id = details.get("profile_id")
        return profile_id if isinstance(profile_id, str) and profile_id else None

    def _current_profile_details(self) -> dict[str, object]:
        """Return resolved current grid profile details."""

        target_count = len(self.gateway_targets)
        ambiguous_gateway = target_count > 1
        target = (
            next(iter(self.gateway_targets.values())) if target_count == 1 else None
        )
        profile_id = (
            None
            if ambiguous_gateway
            else (
                target.current_profile_id
                if target is not None
                else self.current_profile_id
            )
        )
        profile_name = (
            None
            if ambiguous_gateway
            else (
                target.current_profile_name
                if target is not None
                else self.current_profile_name
            )
        )
        source = (
            "ambiguous_gateway"
            if ambiguous_gateway
            else ("gateway" if target is not None else "activation_record")
        )
        profile = self.profile_for_id_in_region(profile_id, self.site_region_code)
        if profile is not None:
            profile_name = profile_name or profile.name
        return {
            "profile_id": profile_id,
            "profile_name": profile_name,
            "profile_group": profile.group if profile is not None else None,
            "profile_country": profile.country if profile is not None else None,
            "profile_region": profile.state if profile is not None else None,
            "pel_enabled": profile.pel_enabled if profile is not None else None,
            "is_277v_compatible": (
                profile.is_277v_compatible if profile is not None else None
            ),
            "recommended": profile.recommended if profile is not None else None,
            "source": source,
            "country_code": self.country_code,
            "region_code": self.site_region_code,
            "support_state": self.support_state,
            "gateway_target_count": target_count,
        }

    def current_profile_attributes(self) -> dict[str, object]:
        """Return concise current grid profile sensor attributes."""

        details = self._current_profile_details()
        return {
            "profile_id": details.get("profile_id"),
            "profile_group": details.get("profile_group"),
            "pel_enabled": details.get("pel_enabled"),
            "is_277v_compatible": details.get("is_277v_compatible"),
            "recommended": details.get("recommended"),
            "source": details.get("source"),
            "country_code": details.get("country_code"),
            "region_code": details.get("region_code"),
            "support_state": details.get("support_state"),
            "gateway_target_count": details.get("gateway_target_count"),
        }

    def profile_id_for_label(self, label: str) -> str | None:
        text = _clean_text(label)
        if not text:
            return None
        for profile in self.filtered_profiles():
            if profile.option_label == text or profile.name == text:
                return profile.profile_id
        return None

    def set_region(self, region_code: str | None) -> None:
        region = self.region_for_code(region_code)
        self.staged_region_code = region.region_code if region is not None else None
        if region is not None and self._derive_region_code() is None:
            self.site_region_code = region.region_code
        self.staged_profile_id = None
        self._publish_state_update()

    def set_list_mode(self, option: str) -> None:
        self.staged_commonly_used = option != ALL_PROFILES_OPTION
        self.staged_profile_id = None
        self._publish_state_update()

    def set_search_query(self, query: str | None) -> None:
        self.staged_query = _clean_text(query) or ""
        self._publish_state_update()

    def set_staged_profile(self, profile_id: str | None) -> None:
        profile = self.profile_for_id(profile_id)
        self.staged_profile_id = profile.profile_id if profile is not None else None
        self._publish_state_update()

    def _derive_country(self) -> str | None:
        for source in (
            self.activation_record,
            getattr(self.coordinator, "battery_country_code", None),
            getattr(self.coordinator, "battery_region", None),
            getattr(self.client, "_system_dashboard_summary_payload", None),
        ):
            country = self._find_country_code(source)
            if country:
                return country
        return None

    async def _async_derive_country(self) -> str | None:
        """Return country metadata, fetching dashboard summary only as fallback."""

        country = self._derive_country()
        if country:
            return country
        fetcher = getattr(self.client, "system_dashboard_summary", None)
        if not callable(fetcher):
            return None
        try:
            payload = await fetcher(allow_reauth=False)
        except Exception:  # noqa: BLE001 - optional metadata fallback
            return None
        return self._find_country_code(payload)

    def _derive_region_code(self) -> str | None:
        for source in (
            self.activation_record,
            getattr(self.coordinator, "battery_region", None),
        ):
            region_code = self._find_region_code(source)
            if region_code:
                return region_code
        return None

    def _find_country_code(self, value: object) -> str | None:
        if isinstance(value, str):
            text = value.strip().upper()
            if len(text) == 2:
                return text
            return None
        if isinstance(value, dict):
            for key in (
                "countryCode",
                "country_code",
                "country",
                "countryCodeAlpha2",
            ):
                country = self._find_country_code(value.get(key))
                if country:
                    return country
            for key in (
                "system",
                "site",
                "address",
                "location",
                "systemAddress",
                "system_address",
            ):
                country = self._find_country_code(value.get(key))
                if country:
                    return country
        elif isinstance(value, list):
            for child in value:
                country = self._find_country_code(child)
                if country:
                    return country
        return None

    def _find_region_code(self, value: object) -> str | None:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            upper_text = text.upper()
            for region in self.regions:
                if region.region_code == upper_text:
                    return region.region_code
                if region.region_name.upper() == upper_text:
                    return region.region_code
                if region.label.upper() == upper_text:
                    return region.region_code
            return None
        if isinstance(value, dict):
            for key in (
                "regionCode",
                "region_code",
                "stateCode",
                "state_code",
                "state",
                "region",
                "province",
            ):
                resolved_region = self._find_region_code(value.get(key))
                if resolved_region:
                    return resolved_region
            for key in (
                "system",
                "site",
                "address",
                "location",
                "systemAddress",
                "system_address",
            ):
                resolved_region = self._find_region_code(value.get(key))
                if resolved_region:
                    return resolved_region
        elif isinstance(value, list):
            for child in value:
                resolved_region = self._find_region_code(child)
                if resolved_region:
                    return resolved_region
        return None

    @staticmethod
    def _walk_dicts(value: object) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        if isinstance(value, dict):
            records.append(value)
            for child in value.values():
                records.extend(GridProfileRuntime._walk_dicts(child))
        elif isinstance(value, list):
            for child in value:
                records.extend(GridProfileRuntime._walk_dicts(child))
        return records

    @staticmethod
    def _envoy_serial_from_record(record: dict[str, object]) -> str | None:
        """Return the Gateway serial from Activation status response shapes."""

        serial = _clean_text(
            record.get("serial_num")
            or record.get("serialNum")
            or record.get("serial_number")
            or record.get("serialNumber")
        )
        if serial:
            return serial
        combiner = record.get("envoyCombiner") or record.get("envoy_combiner")
        if not isinstance(combiner, dict):
            return None
        for serials in combiner.values():
            if isinstance(serials, list):
                for candidate in serials:
                    serial = _clean_text(candidate)
                    if serial:
                        return serial
            else:
                serial = _clean_text(serials)
                if serial:
                    return serial
        return None

    def _parse_reference(self, payload: object) -> None:
        if not isinstance(payload, dict):
            raise ValueError("Activation reference data payload was not an object")
        regions_raw = payload.get("regions") or payload.get("country_regions")
        if not isinstance(regions_raw, dict):
            raise ValueError("Activation reference data missing country regions")
        regions_by_country: dict[str, list[ActivationRegion]] = {}
        for country, region_items in regions_raw.items():
            country_code = _clean_upper(country)
            if country_code is None or not isinstance(region_items, list):
                continue
            regions: list[ActivationRegion] = []
            for item in region_items:
                if not isinstance(item, dict):
                    continue
                region_code = _clean_upper(item.get("regionCode"))
                item_country = _clean_upper(item.get("countryCode")) or country_code
                if not region_code:
                    continue
                region_name = _clean_text(item.get("regionName")) or region_code
                region_id = None
                region_id_text = _clean_text(item.get("id"))
                try:
                    region_id = (
                        int(region_id_text) if region_id_text is not None else None
                    )
                except Exception:  # noqa: BLE001
                    region_id = None
                regions.append(
                    ActivationRegion(
                        country_code=item_country,
                        region_code=region_code,
                        region_name=region_name,
                        region_id=region_id,
                    )
                )
            regions_by_country[country_code] = regions
        self.reference_payload = payload
        self.regions_by_country = regions_by_country

    def _parse_activation_record(self, payload: object) -> None:
        self.activation_record = payload if isinstance(payload, dict) else {}
        root = self.activation_record
        self.current_profile_id = _clean_text(
            root.get("grid_profile")
            or root.get("grid_profile_id")
            or root.get("gridProfileId")
            or root.get("current_grid_profile_id")
            or root.get("currentGridProfileId")
        )
        self.current_profile_name = _clean_text(
            root.get("grid_profile_name")
            or root.get("gridProfileName")
            or root.get("current_grid_profile_name")
            or root.get("currentGridProfileName")
        )
        self.requested_profile_id = _clean_text(
            root.get("requested_profile")
            or root.get("requested_grid_profile_id")
            or root.get("requestedGridProfileId")
        )
        self.requested_profile_name = _clean_text(
            root.get("requested_profile_name")
            or root.get("requested_grid_profile_name")
            or root.get("requestedGridProfileName")
        )
        targets: dict[str, GatewayGridProfileTarget] = {}
        for record in self._walk_dicts(payload):
            serial = _clean_text(
                record.get("serial_num")
                or record.get("serialNum")
                or record.get("serial_number")
                or record.get("serialNumber")
            )
            part = _clean_text(
                record.get("part_num")
                or record.get("partNum")
                or record.get("part_number")
            )
            if not serial:
                continue
            ensemble = _coerce_bool(
                _first_present(
                    record,
                    "ensemble_envoy",
                    "ensembleEnvoy",
                    "is_ensemble",
                )
            )
            if ensemble is None:
                continue
            current_id = _clean_text(
                record.get("grid_profile_id")
                or record.get("gridProfileId")
                or record.get("current_grid_profile_id")
                or record.get("currentGridProfileId")
            )
            requested_id = _clean_text(
                record.get("requested_grid_profile_id")
                or record.get("requestedGridProfileId")
                or record.get("requested_profile_id")
            )
            targets[serial] = GatewayGridProfileTarget(
                serial_num=serial,
                part_num=part,
                ensemble_envoy=ensemble,
                current_profile_id=current_id,
                current_profile_name=_clean_text(
                    record.get("grid_profile_name")
                    or record.get("gridProfileName")
                    or record.get("current_grid_profile_name")
                ),
                requested_profile_id=requested_id,
                requested_profile_name=_clean_text(
                    record.get("requested_grid_profile_name")
                    or record.get("requestedGridProfileName")
                ),
            )
        self.gateway_targets = targets
        if not targets and isinstance(root, dict):
            root_serial = _clean_text(root.get("ensemble_envoy"))
            if root_serial is None:
                serials = root.get("envoy_serial_numbers")
                if isinstance(serials, list) and serials:
                    root_serial = _clean_text(serials[0])
            if root_serial:
                targets[root_serial] = GatewayGridProfileTarget(
                    serial_num=root_serial,
                    part_num=_clean_text(
                        root.get("part_num")
                        or root.get("partNum")
                        or root.get("part_number")
                    ),
                    ensemble_envoy=bool(root.get("ensemble_envoy")),
                    current_profile_id=self.current_profile_id,
                    current_profile_name=self.current_profile_name,
                    requested_profile_id=self.requested_profile_id,
                    requested_profile_name=self.requested_profile_name,
                )
                self.gateway_targets = targets

    def _parse_activation_devices(self, payload: object) -> None:
        """Parse Activation device-list status returned by the cloud endpoint."""

        if isinstance(payload, dict):
            devices = payload.get("devices")
            if not isinstance(devices, list):
                devices = payload.get("envoys")
            records = devices if isinstance(devices, list) else [payload]
        elif isinstance(payload, list):
            records = payload
        else:
            records = []

        targets = dict(self.gateway_targets)
        for record in records:
            if not isinstance(record, dict):
                continue
            grid_profile = record.get("grid_profile") or record.get("envoyGridProfile")
            if not isinstance(grid_profile, dict):
                grid_profile = record
            current_id = _clean_text(
                grid_profile.get("selected_profile_id")
                or grid_profile.get("selectedProfileId")
                or grid_profile.get("grid_profile_id")
                or grid_profile.get("gridProfileId")
                or record.get("grid_profile_id")
                or record.get("gridProfileId")
            )
            current_name = _clean_text(
                grid_profile.get("selected_grid_profile_name")
                or grid_profile.get("selectedGridProfileName")
                or grid_profile.get("grid_profile_name")
                or grid_profile.get("gridProfileName")
                or record.get("grid_profile_name")
                or record.get("gridProfileName")
            )
            requested_present = any(
                key in grid_profile or key in record
                for key in (
                    "requested_profile_id",
                    "requestedProfileId",
                    "requested_grid_profile_name",
                    "requestedGridProfileName",
                )
            )
            requested_id = _clean_text(
                grid_profile.get("requested_profile_id")
                or grid_profile.get("requestedProfileId")
                or record.get("requested_profile_id")
                or record.get("requestedProfileId")
            )
            requested_name = _clean_text(
                grid_profile.get("requested_grid_profile_name")
                or grid_profile.get("requestedGridProfileName")
                or record.get("requested_grid_profile_name")
                or record.get("requestedGridProfileName")
            )
            if current_id:
                self.current_profile_id = current_id
                self.current_profile_name = current_name
            if requested_present:
                self.requested_profile_id = requested_id
                self.requested_profile_name = requested_name
            serial = self._envoy_serial_from_record(record)
            if serial is None and len(targets) == 1:
                serial = next(iter(targets))
            if serial:
                existing = targets.get(serial)
                ensemble = _coerce_bool(
                    _first_present(
                        record,
                        "ensemble_envoy",
                        "ensembleEnvoy",
                        "is_ensemble",
                    )
                )
                if ensemble is None and existing is not None:
                    ensemble = existing.ensemble_envoy
                if ensemble is not None:
                    existing_current_id = (
                        existing.current_profile_id if existing is not None else None
                    )
                    preserve_current_name = (
                        current_id is None or current_id == existing_current_id
                    )
                    targets[serial] = GatewayGridProfileTarget(
                        serial_num=serial,
                        part_num=_clean_text(
                            record.get("part_num")
                            or record.get("partNum")
                            or record.get("part_number")
                        )
                        or (existing.part_num if existing is not None else None),
                        ensemble_envoy=ensemble,
                        current_profile_id=current_id
                        or (
                            existing.current_profile_id
                            if existing is not None
                            else None
                        )
                        or self.current_profile_id,
                        current_profile_name=current_name
                        or (
                            existing.current_profile_name
                            if existing is not None and preserve_current_name
                            else None
                        )
                        or self.current_profile_name,
                        requested_profile_id=(
                            requested_id
                            if requested_present
                            else (
                                existing.requested_profile_id
                                if existing is not None
                                else self.requested_profile_id
                            )
                        ),
                        requested_profile_name=(
                            requested_name
                            if requested_present
                            else (
                                existing.requested_profile_name
                                if existing is not None
                                else self.requested_profile_name
                            )
                        ),
                    )
            elif requested_present:
                self.requested_profile_id = requested_id
                self.requested_profile_name = requested_name
            if (
                self.pending_profile_id
                and current_id
                and (
                    self.pending_gateway_serial is None
                    or serial == self.pending_gateway_serial
                )
                and _profile_id_for_compare(current_id)
                == _profile_id_for_compare(self.pending_profile_id)
            ):
                self._clear_pending_profile()
        self.gateway_targets = targets

    def _parse_profiles(
        self, payload: object, *, country: str, state: str, commonly_used: bool
    ) -> list[GridProfile]:
        if not isinstance(payload, dict):
            raise ValueError("Grid profile payload was not an object")
        grouped = payload.get("grid_profiles")
        if not isinstance(grouped, dict):
            raise ValueError("Grid profile payload missing grid_profiles")
        recommended_id = None
        recommended = payload.get("recommended_profile")
        if isinstance(recommended, dict):
            recommended_id = _clean_text(
                recommended.get("profile_id")
                or recommended.get("grid_profile_id")
                or recommended.get("gridProfileId")
                or recommended.get("id")
            )
        recommended_compare = _profile_id_for_compare(recommended_id)
        profiles: list[GridProfile] = []
        seen: set[str] = set()
        if commonly_used and isinstance(recommended, dict):
            name = _clean_text(recommended.get("name"))
            profile_id = _clean_text(
                recommended.get("profile_id")
                or recommended.get("grid_profile_id")
                or recommended.get("gridProfileId")
                or recommended.get("id")
            )
            if name and profile_id:
                profiles.append(
                    GridProfile(
                        profile_id=profile_id,
                        name=name,
                        group=f"{state}, {country}",
                        country=country,
                        state=state,
                        pel_enabled=_coerce_bool(recommended.get("pel_enabled")),
                        is_277v_compatible=_coerce_bool(
                            recommended.get("is_277v_compatible")
                        ),
                        recommended=True,
                    )
                )
                self.catalog_cache[(country, state, commonly_used)] = (
                    time.monotonic() + 3600.0,
                    profiles,
                )
                return profiles
        for group, items in grouped.items():
            group_label = _clean_text(group) or f"{state}, {country}"
            if not _group_matches_region(group_label, country=country, state=state):
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                profile_id = _clean_text(item.get("profile_id"))
                name = _clean_text(item.get("name"))
                if not profile_id or not name or profile_id in seen:
                    continue
                seen.add(profile_id)
                profiles.append(
                    GridProfile(
                        profile_id=profile_id,
                        name=name,
                        group=group_label,
                        country=country,
                        state=state,
                        pel_enabled=_coerce_bool(item.get("pel_enabled")),
                        is_277v_compatible=_coerce_bool(item.get("is_277v_compatible")),
                        recommended=(
                            recommended_compare is not None
                            and _profile_id_for_compare(profile_id)
                            == recommended_compare
                        ),
                    )
                )
        self.catalog_cache[(country, state, commonly_used)] = (
            time.monotonic() + 3600.0,
            profiles,
        )
        return profiles

    def _mark_denied(self, err: Exception) -> None:
        if self._is_access_denied(err):
            self.support_state = SUPPORT_DENIED
        else:
            self.support_state = SUPPORT_UNAVAILABLE
        self.coordinator._note_endpoint_family_failure(
            ACTIVATION_GRID_PROFILE_FAMILY, err
        )

    @staticmethod
    def _is_access_denied(err: Exception) -> bool:
        if isinstance(err, aiohttp.ClientResponseError) and err.status in {
            401,
            403,
            404,
        }:
            return True
        return isinstance(err, (ActivationAccessDenied, Unauthorized))

    async def async_refresh_device_status(
        self, *, force: bool = False
    ) -> GridProfileBrowseResult:
        """Refresh only the preferred cloud current-profile status endpoint."""

        async with self._lock:
            family = ACTIVATION_GRID_PROFILE_FAMILY
            if not self.coordinator._endpoint_family_should_run(family, force=force):
                return self.browse()
            try:
                devices = await self.client.async_get_activation_device_list()
                self._parse_activation_devices(devices)
            except Exception as err:  # noqa: BLE001
                if (
                    self._is_access_denied(err)
                    or not self.installer_access_ever_confirmed
                ):
                    self._mark_denied(err)
                else:
                    self.support_state = SUPPORT_CONFIRMED
                    self.coordinator._note_endpoint_family_failure(family, err)
                self._publish_state_update()
                return self.browse()
            self.support_state = SUPPORT_CONFIRMED
            self.installer_access_ever_confirmed = True
            self.coordinator._note_endpoint_family_success(family, success_ttl_s=300.0)
            self._publish_state_update()
            return self.browse()

    async def async_refresh(
        self, *, force: bool = False, load_profiles: bool = True
    ) -> GridProfileBrowseResult:
        async with self._lock:
            family = ACTIVATION_GRID_PROFILE_FAMILY
            if not self.coordinator._endpoint_family_should_run(family, force=force):
                return self.browse()
            errors: list[Exception] = []
            successful_requests = 0
            if force or self.reference_payload is None:
                try:
                    reference = await self.client.async_get_activation_reference_data()
                    self._parse_reference(reference)
                    successful_requests += 1
                except Exception as err:  # noqa: BLE001
                    errors.append(err)
            try:
                record = await self.client.async_get_activation_record()
                self._parse_activation_record(record)
                successful_requests += 1
            except Exception as err:  # noqa: BLE001
                errors.append(err)
            try:
                devices = await self.client.async_get_activation_device_list()
                self._parse_activation_devices(devices)
                successful_requests += 1
            except Exception as err:  # noqa: BLE001
                errors.append(err)
            denied_error = next(
                (err for err in errors if self._is_access_denied(err)), None
            )
            if denied_error is not None:
                self._mark_denied(denied_error)
                self._publish_state_update()
                return self.browse()
            if errors and (
                successful_requests == 0 or not self.installer_access_ever_confirmed
            ):
                self._mark_denied(errors[0])
                self._publish_state_update()
                return self.browse()
            self.country_code = await self._async_derive_country()
            derived_region_code = self._derive_region_code()
            if derived_region_code:
                if (
                    self.staged_region_code is None
                    or self.region_for_code(self.staged_region_code) is None
                ):
                    self.staged_region_code = derived_region_code
                self.site_region_code = derived_region_code
            else:
                if (
                    self.staged_region_code is not None
                    and self.region_for_code(self.staged_region_code) is None
                ):
                    self.staged_region_code = None
                if (
                    self.site_region_code is not None
                    and self.region_for_code(self.site_region_code) is None
                ):
                    self.site_region_code = None
            self.support_state = SUPPORT_CONFIRMED
            self.installer_access_ever_confirmed = True
            self.coordinator._note_endpoint_family_success(family, success_ttl_s=300.0)
            loaded_catalogs: set[tuple[str, str, bool]] = set()
            if load_profiles and self.staged_region_code:
                await self.async_load_profiles(force=force)
                if self.country_code:
                    loaded_catalogs.add(
                        (
                            self.country_code,
                            self.staged_region_code,
                            self.staged_commonly_used,
                        )
                    )
            current_target = next(iter(self.gateway_targets.values()), None)
            current_profile_id = (
                current_target.current_profile_id
                if current_target is not None
                else self.current_profile_id
            )
            if (
                load_profiles
                and current_profile_id
                and self.country_code
                and self.site_region_code
                and self.profile_for_id_in_region(
                    current_profile_id, self.site_region_code
                )
                is None
            ):
                for mode in (True, False):
                    key = (self.country_code, self.site_region_code, mode)
                    if key not in loaded_catalogs:
                        await self.async_load_profiles(
                            region_code=self.site_region_code,
                            commonly_used=mode,
                            force=force if load_profiles else False,
                        )
                    if not self.installer_access_confirmed:
                        break
                    if (
                        self.profile_for_id_in_region(
                            current_profile_id, self.site_region_code
                        )
                        is not None
                    ):
                        break
            self._publish_state_update()
            return self.browse()

    async def async_load_profiles(
        self,
        *,
        region_code: str | None = None,
        commonly_used: bool | None = None,
        force: bool = False,
    ) -> list[GridProfile]:
        if not self.installer_access_confirmed:
            return []
        country = self.country_code
        state = _clean_upper(region_code) or self.staged_region_code
        mode = (
            self.staged_commonly_used if commonly_used is None else bool(commonly_used)
        )
        if not country or not state:
            return []
        if self.region_for_code(state) is None:
            self._raise_unavailable(
                "grid_profile_region_invalid",
                "Selected grid profile region is not available.",
            )
        key = (country, state, mode)
        cached = self.catalog_cache.get(key)
        now = time.monotonic()
        if not force and cached and cached[0] > now:
            return list(cached[1])
        if force:
            self.catalog_cache.pop(key, None)
        try:
            payload = await self.client.async_get_grid_profiles_filtered(
                country=country,
                state=state,
                commonly_used=mode,
            )
            profiles = self._parse_profiles(
                payload, country=country, state=state, commonly_used=mode
            )
        except Exception as err:  # noqa: BLE001
            self._mark_denied(err)
            self._publish_state_update()
            return []
        self.coordinator._note_endpoint_family_success(
            ACTIVATION_GRID_PROFILE_FAMILY,
            success_ttl_s=300.0,
        )
        self._publish_state_update()
        return profiles

    def filtered_regions(self, query: str | None = None) -> list[ActivationRegion]:
        query_text = (_clean_text(query) or self.staged_query).lower()
        if not query_text:
            return self.regions
        return [
            region
            for region in self.regions
            if query_text in region.region_code.lower()
            or query_text in region.region_name.lower()
            or query_text in f"{region.region_code}, {region.country_code}".lower()
        ]

    def filtered_profiles(self, query: str | None = None) -> list[GridProfile]:
        country = self.country_code
        state = self.staged_region_code
        if not country or not state:
            return []
        cached = self.catalog_cache.get((country, state, self.staged_commonly_used))
        profiles = list(cached[1]) if cached else []
        query_text = (_clean_text(query) or self.staged_query).lower()
        if not query_text:
            return profiles
        return [
            profile
            for profile in profiles
            if query_text in profile.name.lower()
            or query_text in profile.profile_id.lower()
            or query_text in profile.group.lower()
        ]

    def browse(
        self,
        *,
        region_code: str | None = None,
        query: str | None = None,
        commonly_used: bool | None = None,
    ) -> GridProfileBrowseResult:
        previous_region = self.staged_region_code
        previous_query = self.staged_query
        previous_mode = self.staged_commonly_used
        if region_code is not None:
            self.staged_region_code = _clean_upper(region_code)
        if query is not None:
            self.staged_query = _clean_text(query) or ""
        if commonly_used is not None:
            self.staged_commonly_used = bool(commonly_used)
        profiles = self.filtered_profiles()
        grouped: dict[str, list[dict[str, object]]] = {}
        for profile in profiles:
            grouped.setdefault(profile.group, []).append(_profile_dict(profile))
        target = (
            next(iter(self.gateway_targets.values()))
            if len(self.gateway_targets) == 1
            else None
        )
        result = GridProfileBrowseResult(
            support_state=self.support_state,
            country_code=self.country_code,
            regions=[_region_dict(region) for region in self.filtered_regions()],
            profiles=[_profile_dict(profile) for profile in profiles],
            grouped_profiles=grouped,
            current_profile=(
                {
                    "profile_id": target.current_profile_id,
                    "name": target.current_profile_name,
                }
                if target and (target.current_profile_id or target.current_profile_name)
                else None
            ),
            requested_profile=(
                {
                    "profile_id": target.requested_profile_id,
                    "name": target.requested_profile_name,
                }
                if target
                and (target.requested_profile_id or target.requested_profile_name)
                else None
            ),
            staged={
                "region_code": self.staged_region_code,
                "commonly_used": self.staged_commonly_used,
                "query": self.staged_query,
                "profile_id": self.staged_profile_id,
                "profile_label": self.staged_profile_label,
            },
        )
        self.staged_region_code = previous_region
        self.staged_query = previous_query
        self.staged_commonly_used = previous_mode
        return result

    def browse_dict(self, **kwargs: Any) -> dict[str, object]:
        result = self.browse(**kwargs)
        return {
            "support_state": result.support_state,
            "country_code": result.country_code,
            "regions": result.regions,
            "profiles": result.profiles,
            "grouped_profiles": result.grouped_profiles,
            "current_profile": result.current_profile,
            "requested_profile": result.requested_profile,
            "staged": result.staged,
        }

    def _target_for_serial(
        self, gateway_serial: str | None = None
    ) -> GatewayGridProfileTarget | None:
        if gateway_serial:
            return self.gateway_targets.get(gateway_serial)
        if len(self.gateway_targets) == 1:
            return next(iter(self.gateway_targets.values()))
        return None

    def _raise_unavailable(self, key: str, message: str) -> None:
        raise_translated_service_validation(
            translation_domain=DOMAIN,
            translation_key=key,
            message=message,
        )

    def _clear_pending_profile(self) -> None:
        self.pending_profile_id = None
        self.pending_gateway_serial = None
        self.pending_started_mono = None
        task = self._pending_refresh_task
        if task is not None and not task.done():
            task.cancel()
        self._pending_refresh_task = None

    def cancel_pending_refresh(self) -> None:
        """Cancel any background pending-profile refresh task."""

        task = self._pending_refresh_task
        if task is not None and not task.done():
            task.cancel()
        self._pending_refresh_task = None

    async def _async_poll_pending_profile(self, profile_id: str) -> None:
        deadline = time.monotonic() + self._pending_poll_window_s
        try:
            while (
                self.pending_profile_id
                and self.support_state != SUPPORT_DENIED
                and _profile_id_for_compare(self.pending_profile_id)
                == _profile_id_for_compare(profile_id)
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(self._pending_poll_interval_s)
                if not self.pending_profile_id or self.support_state == SUPPORT_DENIED:
                    break
                await self.async_refresh_device_status(force=True)
        except asyncio.CancelledError:
            raise
        finally:
            pending_matches = (
                self.pending_profile_id is not None
                and _profile_id_for_compare(self.pending_profile_id)
                == _profile_id_for_compare(profile_id)
            )
            if pending_matches and (
                self.support_state == SUPPORT_DENIED or time.monotonic() >= deadline
            ):
                self.pending_profile_id = None
                self.pending_gateway_serial = None
                self.pending_started_mono = None
                self._publish_state_update()
            if self._pending_refresh_task is asyncio.current_task():
                self._pending_refresh_task = None

    def _start_pending_refresh(self, profile_id: str) -> None:
        self.cancel_pending_refresh()
        hass = getattr(self.coordinator, "hass", None)
        create_task = getattr(hass, "async_create_task", None)
        if not callable(create_task):
            return
        task = create_task(
            self._async_poll_pending_profile(profile_id),
            name=f"{DOMAIN}_grid_profile_pending_refresh",
        )
        if isinstance(task, asyncio.Task):
            self._pending_refresh_task = task

    async def async_apply_staged(self) -> dict[str, object]:
        return await self.async_apply_grid_profile(
            self.staged_profile_id,
            region_code=self.staged_region_code,
        )

    async def async_apply_grid_profile(
        self,
        profile_id: str | None,
        *,
        region_code: str | None = None,
        gateway_serial: str | None = None,
    ) -> dict[str, object]:
        """Apply one grid profile while serializing writes for this site."""

        async with self._apply_lock:
            return await self._async_apply_grid_profile_locked(
                profile_id,
                region_code=region_code,
                gateway_serial=gateway_serial,
            )

    async def _async_apply_grid_profile_locked(
        self,
        profile_id: str | None,
        *,
        region_code: str | None = None,
        gateway_serial: str | None = None,
    ) -> dict[str, object]:
        if not self.installer_access_confirmed:
            if self.support_state != SUPPORT_DENIED:
                self._raise_unavailable(
                    "grid_profile_unavailable",
                    "Grid profile control is unavailable.",
                )
            self._raise_unavailable(
                "grid_profile_installer_required",
                "Grid profile control requires installer-level Enphase Activation access.",
            )
        strict_region = _clean_upper(region_code)
        if strict_region and self.region_for_code(strict_region) is None:
            self._raise_unavailable(
                "grid_profile_region_invalid",
                "Selected grid profile region is not available.",
            )
        profile = (
            self.profile_for_id_in_region(profile_id, strict_region)
            if strict_region
            else self.profile_for_id(profile_id)
        )
        if profile is None and strict_region:
            await self.async_load_profiles(
                region_code=strict_region,
                commonly_used=True,
                force=True,
            )
            profile = self.profile_for_id_in_region(profile_id, strict_region)
            if profile is None:
                await self.async_load_profiles(
                    region_code=strict_region,
                    commonly_used=False,
                    force=True,
                )
                profile = self.profile_for_id_in_region(profile_id, strict_region)
        if profile is None:
            self._raise_unavailable(
                "grid_profile_profile_invalid",
                "Selected grid profile is not available for this site country and region.",
            )
        target = self._target_for_serial(gateway_serial)
        if target is None:
            self._raise_unavailable(
                "grid_profile_gateway_required",
                "Grid profile apply requires a cloud Activation Gateway record.",
            )
        assert profile is not None
        assert target is not None
        try:
            await self.client.async_apply_grid_profile(
                gateway_serial=target.serial_num,
                part_num=target.part_num,
                ensemble_envoy=target.ensemble_envoy,
                profile_id=profile.profile_id,
            )
        except ServiceValidationError:
            raise
        except Exception as err:  # noqa: BLE001
            self._mark_denied(err)
            self._raise_unavailable(
                "grid_profile_apply_failed",
                "Grid profile apply failed.",
            )
        self.pending_profile_id = profile.profile_id
        self.pending_gateway_serial = target.serial_num
        self.pending_started_mono = time.monotonic()
        self._start_pending_refresh(profile.profile_id)
        await self.async_refresh_device_status(force=True)
        refreshed_target = self.gateway_targets.get(target.serial_num, target)
        return {
            "success": True,
            "profile_id": profile.profile_id,
            "profile_name": profile.name,
            "selected_profile_id": refreshed_target.current_profile_id,
            "requested_profile_id": profile.profile_id,
            "cloud_apply_status": "accepted",
        }

    def diagnostics(self) -> dict[str, object]:
        profiles = self.filtered_profiles()
        return {
            "support_state": self.support_state,
            "country_code": self.country_code,
            "site_region_code": self.site_region_code,
            "region_count": len(self.regions),
            "staged_region_code": self.staged_region_code,
            "staged_commonly_used": self.staged_commonly_used,
            "profile_count": len(profiles),
            "pending_profile": bool(self.pending_profile_id),
            "gateway_target_count": len(self.gateway_targets),
            "profiles": [
                {
                    "profile_id": "[redacted]",
                    "name": profile.name,
                    "group": profile.group,
                    "pel_enabled": profile.pel_enabled,
                }
                for profile in profiles[:20]
            ],
        }
