"""Resolve Home Assistant service targets to loaded Enphase runtimes."""

from __future__ import annotations
from typing import TYPE_CHECKING
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service as ha_service
from homeassistant.helpers import target as ha_target
from .const import DOMAIN
from .device_types import parse_type_identifier
from .device_registry_compat import device_config_entry_ids
from .runtime_data import iter_coordinators, loaded_runtime_data
from homeassistant.exceptions import ServiceValidationError

if TYPE_CHECKING:
    from .coordinator import EnphaseCoordinator


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
        return self._site_id_from_device(dev_reg, dev)

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

    def _extract_target_references(
        self, call: ServiceCall
    ) -> tuple[set[str], set[str], set[str], bool]:
        referenced_entity_ids: set[str] = set()
        indirect_entity_ids: set[str] = set()
        device_ids: set[str] = set()
        extractor = getattr(ha_target, "async_extract_referenced_entity_ids", None)
        target_selection = getattr(ha_target, "TargetSelection", None)
        if not callable(extractor):
            return (referenced_entity_ids, indirect_entity_ids, device_ids, False)
        try:
            selection = (
                target_selection(call.data) if callable(target_selection) else call
            )
            selected = extractor(self.hass, selection)
        except Exception:
            return (referenced_entity_ids, indirect_entity_ids, device_ids, False)
        referenced = getattr(selected, "referenced", None)
        indirectly_referenced = getattr(selected, "indirectly_referenced", None)
        referenced_devices = getattr(selected, "referenced_devices", None)
        if referenced is None and indirectly_referenced is None:
            referenced_entity_ids |= {str(entity_id) for entity_id in selected}
        else:
            referenced_entity_ids |= {str(entity_id) for entity_id in referenced or ()}
            indirect_entity_ids |= {
                str(entity_id) for entity_id in indirectly_referenced or ()
            }
        device_ids |= {str(device_id) for device_id in referenced_devices or ()}
        return (referenced_entity_ids, indirect_entity_ids, device_ids, True)

    def _extract_device_ids(self, call: ServiceCall) -> list[str]:
        _entity_ids, _indirect_entity_ids, device_ids, _extracted = (
            self._extract_target_references(call)
        )
        extractor = getattr(ha_service, "async_extract_referenced_device_ids", None)
        if callable(extractor):
            try:
                device_ids |= {str(value) for value in extractor(self.hass, call)}
            except Exception:
                pass
        data_ids = call.data.get("device_id")
        if data_ids:
            if isinstance(data_ids, str):
                device_ids.add(data_ids)
            else:
                device_ids |= {str(v) for v in data_ids}
        ent_reg = er.async_get(self.hass)
        for entity_id in self._extract_entity_ids(call, include_indirect=True):
            reg_entry = ent_reg.async_get(entity_id)
            if (
                reg_entry is not None
                and reg_entry.platform == DOMAIN
                and reg_entry.device_id
            ):
                device_ids.add(reg_entry.device_id)
        return list(device_ids)

    def _extract_entity_ids(
        self, call: ServiceCall, *, include_indirect: bool = False
    ) -> list[str]:
        entity_ids, indirect_entity_ids, _device_ids, extracted = (
            self._extract_target_references(call)
        )
        if include_indirect:
            entity_ids |= indirect_entity_ids
        if not extracted:
            extractor = getattr(ha_service, "async_extract_referenced_entity_ids", None)
            if callable(extractor):
                try:
                    entity_ids |= {
                        str(entity_id) for entity_id in extractor(self.hass, call)
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

    async def _resolve_site_ids_from_call(self, call: ServiceCall) -> set[str]:
        site_ids: set[str] = set()
        for device_id in self._extract_device_ids(call):
            site_id = await self._resolve_site_id(device_id)
            if site_id:
                site_ids.add(site_id)
        ent_reg = er.async_get(self.hass)
        for entity_id in self._extract_entity_ids(call, include_indirect=True):
            reg_entry = ent_reg.async_get(entity_id)
            if reg_entry is None or reg_entry.platform != DOMAIN:
                continue
            device_id = reg_entry.device_id
            if device_id and (site_id := (await self._resolve_site_id(device_id))):
                site_ids.add(site_id)
                continue
            config_entry_id = reg_entry.config_entry_id
            if config_entry_id and (
                coord := self._get_coordinator_for_entry_id(config_entry_id)
            ):
                site_ids.add(str(coord.site_id))
        explicit = call.data.get("site_id")
        if explicit:
            site_ids.add(str(explicit))
        return site_ids

    async def _resolve_single_site_coordinator(
        self, call: ServiceCall
    ) -> EnphaseCoordinator:
        config_entry_id = call.data.get("config_entry_id")
        if config_entry_id:
            coord = self._get_coordinator_for_entry_id(str(config_entry_id))
            if coord is not None:
                return coord
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="grid_site_required",
            )
        site_ids = await self._resolve_site_ids_from_call(call)
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
        target = next(iter(site_ids))
        coordinators = iter_coordinators(self.hass, site_ids={target})
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
        device_ids = self._extract_device_ids(call)
        explicit_device_ids = set(device_ids)
        if any((key in call.data for key in ("area_id", "floor_id", "label_id"))):
            direct_call = ServiceCall(
                self.hass,
                call.domain,
                call.service,
                {
                    key: value
                    for key, value in call.data.items()
                    if key not in {"area_id", "floor_id", "label_id"}
                },
            )
            explicit_device_ids = set(self._extract_device_ids(direct_call))
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
