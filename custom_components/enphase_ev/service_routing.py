"""Resolve Home Assistant service targets to loaded Enphase runtimes."""

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from homeassistant.const import CONF_EXCLUDE
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import target as ha_target
from .const import DOMAIN
from .device_types import parse_type_identifier
from .device_registry_helpers import device_config_entry_ids
from .runtime_data import iter_coordinators, loaded_runtime_data
from homeassistant.exceptions import ServiceValidationError

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator


@dataclass(slots=True)
class ResolvedTarget:
    """Keep the native selection and its surviving references for one call."""

    selection: ha_target.TargetSelection
    selected: ha_target.SelectedEntities
    excluded_device_ids: set[str]


class ServiceRouter:
    """Keep registry target interpretation separate from command handlers."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _serial_from_device(self, dev: dr.DeviceEntry) -> str | None:
        for domain, sn in dev.identifiers:
            if domain == DOMAIN:
                if sn.startswith("site:"):
                    continue
                if sn.startswith("type:"):
                    continue
                return str(sn)
        return None

    def _site_id_from_device(
        self, dev_reg: dr.DeviceRegistry, dev: dr.DeviceEntry
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

    async def _resolve_site_id(self, device_id: str) -> str | None:
        dev_reg = dr.async_get(self.hass)
        dev = dev_reg.async_get(device_id)
        if not dev:
            return None
        site_id = self._site_id_from_device(dev_reg, dev)
        if site_id is not None or self._serial_from_device(dev) is None:
            return site_id

        # A retained charger can have no matching entities or inventory parent.
        # Its entry-scoped registry ownership still identifies one loaded site.
        owner_sites = {
            str(coord.site_id)
            for entry_id in self._device_config_entry_ids(dev_reg, dev)
            if (coord := self._get_coordinator_for_entry_id(entry_id)) is not None
        }
        return next(iter(owner_sites)) if len(owner_sites) == 1 else None

    def _iter_loaded_coordinators(self) -> list[EnphaseCoordinator]:
        coordinators: list[EnphaseCoordinator] = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            runtime_data = loaded_runtime_data(entry)
            if runtime_data is not None:
                coordinators.append(runtime_data.coordinator)
        return coordinators

    def _coordinator_has_serial(self, coord: EnphaseCoordinator, sn: str) -> bool:
        data = coord.data if isinstance(getattr(coord, "data", None), dict) else {}
        return sn in (getattr(coord, "serials", None) or set()) or sn in data

    def _coordinator_can_fallback_for_serial(
        self, coord: EnphaseCoordinator, sn: str, site_id: str | None
    ) -> bool:
        if site_id is not None and str(getattr(coord, "site_id", "")) != site_id:
            return False
        if getattr(coord, "site_only", False):
            return False
        serials = getattr(coord, "serials", None) or set()
        data = coord.data if isinstance(getattr(coord, "data", None), dict) else {}
        return bool(not serials and (not data) and sn)

    def _device_config_entry_ids(
        self, dev_reg: dr.DeviceRegistry, device: dr.DeviceEntry
    ) -> list[str]:
        return list(device_config_entry_ids(device, device_registry=dev_reg))

    def _config_entry_ids_for_device(
        self, dev_reg: dr.DeviceRegistry, dev: dr.DeviceEntry
    ) -> list[str]:
        entry_ids = self._device_config_entry_ids(dev_reg, dev)
        if entry_ids:
            return entry_ids
        via = dev.via_device_id
        if not via:
            return []
        parent = dev_reg.async_get(via)
        if not parent:
            return []
        return self._device_config_entry_ids(dev_reg, parent)

    async def _resolve_device_routing_context(
        self, device_id: str
    ) -> tuple[str, str | None, list[str]] | None:
        dev_reg = dr.async_get(self.hass)
        dev = dev_reg.async_get(device_id)
        if not dev:
            return None
        sn = self._serial_from_device(dev)
        if not sn:
            return None
        return (
            sn,
            self._site_id_from_device(dev_reg, dev),
            self._config_entry_ids_for_device(dev_reg, dev),
        )

    async def _get_coordinator_for_sn(
        self,
        sn: str,
        *,
        site_id: str | None = None,
        config_entry_ids: list[str] | None = None,
    ) -> EnphaseCoordinator | None:
        sn = str(sn)
        for entry_id in config_entry_ids or []:
            coord = self._get_coordinator_for_entry_id(entry_id)
            if coord is None:
                continue
            if self._coordinator_has_serial(coord, sn):
                return coord
        all_coordinators = self._iter_loaded_coordinators()
        if site_id is not None:
            site_coordinators = [
                coord
                for coord in all_coordinators
                if str(getattr(coord, "site_id", "")) == site_id
            ]
            exact_matches = [
                coord
                for coord in site_coordinators
                if self._coordinator_has_serial(coord, sn)
            ]
            if len(exact_matches) == 1:
                return exact_matches[0]
            if exact_matches:
                return None
            fallback_candidates = [
                coord
                for coord in site_coordinators
                if self._coordinator_can_fallback_for_serial(coord, sn, site_id)
            ]
            if len(fallback_candidates) == 1:
                return fallback_candidates[0]
            return None
        exact_matches = [
            coord
            for coord in all_coordinators
            if self._coordinator_has_serial(coord, sn)
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if exact_matches:
            return None
        fallback_candidates = [
            coord
            for coord in all_coordinators
            if self._coordinator_can_fallback_for_serial(coord, sn, None)
        ]
        if len(fallback_candidates) == 1:
            return fallback_candidates[0]
        return None

    def _get_coordinator_for_entry_id(self, entry_id: str) -> EnphaseCoordinator | None:
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id != entry_id:
                continue
            runtime_data = loaded_runtime_data(entry)
            if runtime_data is not None:
                return runtime_data.coordinator
        return None

    def _resolve_target(
        self, call: ServiceCall, *, primary_entities_only: bool = True
    ) -> ResolvedTarget:
        selection = ha_target.TargetSelection(call.data)
        selected = ha_target.async_extract_referenced_entity_ids(
            self.hass,
            selection,
            primary_entities_only=primary_entities_only,
        )
        excluded_device_ids: set[str] = set()
        if selection.exclude is not None:
            excluded_device_ids = ha_target.async_extract_referenced_entity_ids(
                self.hass, selection.exclude, primary_entities_only=False
            ).referenced_devices
        return ResolvedTarget(selection, selected, excluded_device_ids)

    def _has_target(self, call: ServiceCall) -> bool:
        """Distinguish an omitted target from an explicit empty selection."""
        return not call.data.keys().isdisjoint(cv.TARGET_ID_KEYS)

    def _enphase_device_ids_for_entities(self, entity_ids: set[str]) -> set[str]:
        ent_reg = er.async_get(self.hass)
        return {
            reg_entry.device_id
            for entity_id in entity_ids
            if (reg_entry := ent_reg.async_get(entity_id)) is not None
            and reg_entry.platform == DOMAIN
            and reg_entry.device_id
        }

    def _device_ids_from_target_references(self, target: ResolvedTarget) -> set[str]:
        selected = target.selected
        selected_device_ids: set[str] = selected.referenced_devices
        entity_devices = self._enphase_device_ids_for_entities(
            selected.referenced | selected.indirectly_referenced
        )
        # An entity can belong to a different area from its device. It may
        # survive a Core entity selection even when its device was excluded.
        return (selected_device_ids | entity_devices) - target.excluded_device_ids

    def _extract_tariff_entity_ids(
        self, target: ResolvedTarget, *, site_id: str | None
    ) -> set[str]:
        selected = target.selected
        directly_named: set[str] = target.selection.entity_ids & selected.referenced
        if directly_named:
            if site_id is None:
                return directly_named
            return {
                entity_id
                for entity_id in directly_named
                if (coord := self._coordinator_from_tariff_entity(entity_id))
                is not None
                and str(coord.site_id) == site_id
            }
        return {
            entity_id
            for entity_id in selected.referenced | selected.indirectly_referenced
            if (coord := self._coordinator_from_tariff_entity(entity_id)) is not None
            and (site_id is None or str(coord.site_id) == site_id)
            and (state := self.hass.states.get(entity_id)) is not None
            and isinstance(state.attributes.get("tariff_locator"), dict)
        }

    def _extract_excluded_entity_ids(self, target: ResolvedTarget) -> set[str]:
        return cast(
            set[str],
            ha_target.async_extract_excluded_entity_ids(self.hass, target.selection),
        )

    async def _resolve_site_ids_from_call(
        self,
        call: ServiceCall,
        *,
        target: ResolvedTarget | None = None,
        restrict_to_target: bool = False,
    ) -> set[str]:
        if target is None:
            target = self._resolve_target(call)
        selected = target.selected
        site_ids: set[str] = set()
        for device_id in self._device_ids_from_target_references(target):
            site_id = await self._resolve_site_id(device_id)
            if site_id:
                site_ids.add(site_id)
        ent_reg = er.async_get(self.hass)
        for entity_id in selected.referenced | selected.indirectly_referenced:
            reg_entry = ent_reg.async_get(entity_id)
            if reg_entry is None or reg_entry.platform != DOMAIN:
                continue
            device_id = reg_entry.device_id
            if device_id in target.excluded_device_ids:
                continue
            if device_id and (site_id := (await self._resolve_site_id(device_id))):
                site_ids.add(site_id)
                continue
            config_entry_id = reg_entry.config_entry_id
            if config_entry_id and (
                coord := self._get_coordinator_for_entry_id(config_entry_id)
            ):
                site_ids.add(str(coord.site_id))
        explicit = call.data.get("site_id")
        if restrict_to_target or call.data.get(CONF_EXCLUDE):
            if explicit:
                site_ids.intersection_update({str(explicit)})
            if not site_ids:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="grid_site_required"
                )
        elif explicit:
            site_ids.add(str(explicit))
        return site_ids

    async def _resolve_single_site_coordinator(
        self,
        call: ServiceCall,
        *,
        target: ResolvedTarget | None = None,
        restrict_to_target: bool = False,
    ) -> EnphaseCoordinator:
        config_entry_id = call.data.get("config_entry_id")
        if config_entry_id:
            coord = self._get_coordinator_for_entry_id(str(config_entry_id))
            if coord is not None and (
                not (restrict_to_target or call.data.get(CONF_EXCLUDE))
                or str(coord.site_id)
                in await self._resolve_site_ids_from_call(
                    call,
                    target=target,
                    restrict_to_target=restrict_to_target,
                )
            ):
                return coord
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="grid_site_required",
            )
        site_ids = await self._resolve_site_ids_from_call(
            call,
            target=target,
            restrict_to_target=restrict_to_target,
        )
        if not site_ids:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="grid_site_required"
            )
        if len(site_ids) > 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="grid_site_ambiguous",
                translation_placeholders={"count": str(len(site_ids))},
            )
        site_id = next(iter(site_ids))
        coordinators = iter_coordinators(self.hass, site_ids={site_id})
        if coordinators:
            return coordinators[0]
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="grid_site_required"
        )

    def _coordinator_from_tariff_entity(
        self, entity_id: str
    ) -> EnphaseCoordinator | None:
        ent_reg = er.async_get(self.hass)
        reg_entry = ent_reg.async_get(entity_id)
        if reg_entry is None:
            return None
        entry_domain = getattr(reg_entry, "domain", entity_id.partition(".")[0])
        if reg_entry.platform != DOMAIN or entry_domain not in {"sensor", "number"}:
            return None
        unique_id = str(reg_entry.unique_id or "")
        if not any(
            (
                token in unique_id
                for token in (
                    "_tariff_import_rate_",
                    "_tariff_export_rate_",
                    "_tariff_current_import_rate",
                    "_tariff_current_export_rate",
                )
            )
        ):
            return None
        config_entry_id = getattr(reg_entry, "config_entry_id", None)
        if config_entry_id:
            coord = self._get_coordinator_for_entry_id(str(config_entry_id))
            if coord is not None:
                return coord
        for coord in self._iter_loaded_coordinators():
            if f"{DOMAIN}_site_{coord.site_id}_" in unique_id:
                return coord
        return None

    async def _resolve_charger_targets(
        self, call: ServiceCall
    ) -> list[tuple[str, str, EnphaseCoordinator]]:
        target = self._resolve_target(call)
        device_ids = self._device_ids_from_target_references(target)
        # Core puts group members in referenced. Only entity IDs named in
        # the call require a charger; skip other devices reached by a group.
        explicit_device_ids = self._enphase_device_ids_for_entities(
            target.selection.entity_ids & target.selected.referenced
        )
        selection = target.selection
        if selection.device_ids:
            if selection.area_ids or selection.floor_ids or selection.label_ids:
                direct_devices = ha_target.TargetSelection(
                    {"device_id": list(selection.device_ids)}
                )
                explicit_device_ids.update(
                    ha_target.async_extract_referenced_entity_ids(
                        self.hass, direct_devices
                    ).referenced_devices
                )
            else:
                explicit_device_ids.update(target.selected.referenced_devices)
        targets: list[tuple[str, str, EnphaseCoordinator]] = []
        for device_id in device_ids:
            routing_context = await self._resolve_device_routing_context(device_id)
            if routing_context is None:
                if device_id not in explicit_device_ids:
                    continue
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="grid_site_required"
                )
            sn, site_id, config_entry_ids = routing_context
            coord = await self._get_coordinator_for_sn(
                sn, site_id=site_id, config_entry_ids=config_entry_ids
            )
            if coord is None:
                if device_id not in explicit_device_ids:
                    continue
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="grid_site_required"
                )
            targets.append((device_id, sn, coord))
        if not targets:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="grid_site_required"
            )
        return targets
