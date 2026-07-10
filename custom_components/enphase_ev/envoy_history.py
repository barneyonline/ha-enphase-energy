from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers import entity_registry as er

from .const import CONF_SITE_ID, DOMAIN
from .runtime_data import EnphaseConfigEntry

_recorder_statistics_module: Any | None = None


def _load_recorder_statistics() -> Any:
    global _recorder_statistics_module

    if _recorder_statistics_module is None:
        from homeassistant.components.recorder import statistics

        _recorder_statistics_module = statistics
    return _recorder_statistics_module


class _RecorderStatisticsProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_load_recorder_statistics(), name)


recorder_statistics: Any = _RecorderStatisticsProxy()
ENVOY_DOMAIN = "enphase_envoy"
MIGRATION_FLOWS: tuple[str, ...] = (
    "solar_production",
    "consumption",
    "grid_import",
    "grid_export",
    "battery_charge",
    "battery_discharge",
)
FLOW_LABELS: dict[str, str] = {
    "solar_production": "Site Solar Production",
    "consumption": "Site Consumption",
    "grid_import": "Site Grid Import",
    "grid_export": "Site Grid Export",
    "battery_charge": "Site Battery Charge",
    "battery_discharge": "Site Battery Discharge",
}
ALLOWED_STATE_CLASSES = {"total", "total_increasing"}
LOW_VALUE_TOLERANCE_KWH = 0.01
_OPTION_SKIP = "__skip__"
_EXCLUDED_OBJECT_ID_TERMS = ("daily", "today", "current", "power")
_PHASE_OBJECT_ID_PATTERN = re.compile(r"(^|_)(l[123]|phase_[a-z0-9]+)$")
_UNIT_TO_KWH: dict[str, float] = {
    str(UnitOfEnergy.WATT_HOUR): 0.001,
    "wh": 0.001,
    str(UnitOfEnergy.KILO_WATT_HOUR): 1.0,
    "kwh": 1.0,
    str(UnitOfEnergy.MEGA_WATT_HOUR): 1000.0,
    "mwh": 1000.0,
}


@dataclass(slots=True)
class EnvoyHistoryCandidate:
    entity_id: str
    config_entry_id: str | None
    title: str
    current_value_kwh: float
    platform: str | None = None


@dataclass(slots=True)
class EnvoyHistorySource:
    entry_id: str
    title: str
    candidates: list[EnvoyHistoryCandidate]

    def candidate_by_entity_id(self) -> dict[str, EnvoyHistoryCandidate]:
        return {candidate.entity_id: candidate for candidate in self.candidates}


@dataclass(slots=True)
class EnvoyHistoryTarget:
    flow_key: str
    label: str
    unique_id: str
    entity_id: str
    current_value_kwh: float | None


@dataclass(slots=True)
class EnvoyHistoryMapping:
    flow_key: str
    label: str
    old_entity_id: str
    archived_entity_id: str
    old_value_kwh: float
    new_entity_id: str
    new_value_kwh: float
    target_unique_id: str


@dataclass(slots=True)
class EnvoyHistoryWarning:
    flow_key: str
    label: str
    old_entity_id: str
    old_value_kwh: float
    new_entity_id: str
    new_value_kwh: float


@dataclass(slots=True)
class EnvoyHistoryValidation:
    error: str | None
    mappings: list[EnvoyHistoryMapping]
    warnings: list[EnvoyHistoryWarning] = field(default_factory=list)


@dataclass(slots=True)
class EnvoyHistoryExecutionError:
    completed: list[EnvoyHistoryMapping]
    failed: EnvoyHistoryMapping | None
    reason: str


def migration_flow_fields() -> tuple[str, ...]:
    return MIGRATION_FLOWS


def skip_option_value() -> str:
    return _OPTION_SKIP


def migration_target_unique_id(site_id: str, flow_key: str) -> str:
    return f"{DOMAIN}_site_{site_id}_{flow_key}"


def _archive_entity_id(
    entity_id: str,
    current_ids: set[str],
    platform: str | None,
) -> str:
    domain, _, object_id = entity_id.partition(".")
    if not domain or not object_id:
        return entity_id
    suffix = "envoy_legacy" if platform == ENVOY_DOMAIN else "legacy"
    archive_name = f"{object_id}_{suffix}"
    archive_entity_id = async_generate_entity_id(
        f"{domain}.{{}}",
        archive_name,
        current_ids=current_ids,
    )
    current_ids.add(archive_entity_id)
    return str(archive_entity_id)


def _normalize_text(value: object) -> str:
    try:
        text = str(value).strip().lower()
    except Exception:  # noqa: BLE001
        return ""
    return text


def _normalize_entity_tokens(entity_id: str) -> str:
    normalized = entity_id.partition(".")[2].lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    return normalized.strip("_")


def _normalize_title_tokens(title: str) -> str:
    normalized = title.partition(" (")[0].lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    return normalized.strip("_")


def _unit_to_kwh_factor(unit: object) -> float | None:
    normalized = _normalize_text(unit)
    if not normalized:
        return None
    return _UNIT_TO_KWH.get(normalized)


def _state_value_kwh(state: State | None) -> float | None:
    if state is None:
        return None
    factor = _unit_to_kwh_factor(state.attributes.get("unit_of_measurement"))
    if factor is None:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return round(value * factor, 6)


def _is_compatible_energy_total_state(state: State | None) -> bool:
    if state is None:
        return False
    device_class = _normalize_text(state.attributes.get("device_class"))
    if device_class != "energy":
        return False
    state_class = _normalize_text(state.attributes.get("state_class"))
    if state_class not in ALLOWED_STATE_CLASSES:
        return False
    return _state_value_kwh(state) is not None


def _friendly_title(state: State | None, entity_id: str) -> str:
    friendly_name = state.attributes.get("friendly_name") if state is not None else None
    if isinstance(friendly_name, str) and friendly_name.strip():
        return f"{friendly_name.strip()} ({entity_id})"
    return entity_id


def _friendly_title_from_name(name: object, entity_id: str) -> str:
    if isinstance(name, str) and name.strip():
        return f"{name.strip()} ({entity_id})"
    return entity_id


def _statistics_unit(metadata: dict[str, Any]) -> object:
    return metadata.get("statistics_unit_of_measurement") or metadata.get(
        "display_unit_of_measurement"
    )


def _recorder_statistics() -> Any:
    return recorder_statistics


async def _statistics_metadata_by_id(
    hass: HomeAssistant, statistic_ids: set[str]
) -> dict[str, dict[str, Any]]:
    if not statistic_ids:
        return {}
    recorder_statistics = _recorder_statistics()
    try:
        rows = await recorder_statistics.async_list_statistic_ids(
            hass,
            statistic_ids=statistic_ids,
            statistic_type="sum",
        )
    except Exception:  # noqa: BLE001
        return {}
    return {
        row["statistic_id"]: row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("statistic_id"), str)
    }


async def _last_statistic_value_kwh(
    hass: HomeAssistant, statistic_id: str, unit: object
) -> float | None:
    factor = _unit_to_kwh_factor(unit)
    if factor is None:
        return None
    recorder_statistics = _recorder_statistics()
    try:
        result = await hass.async_add_executor_job(
            recorder_statistics.get_last_statistics,
            hass,
            1,
            statistic_id,
            False,
            {"sum", "state"},
        )
    except Exception:  # noqa: BLE001
        return None
    rows = result.get(statistic_id) or []
    if not rows:
        return None
    latest = rows[-1]
    raw_value = latest.get("state")
    if raw_value is None:
        raw_value = latest.get("sum")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return round(value * factor, 6)


async def _candidate_from_registry_entry(
    hass: HomeAssistant,
    reg_entry: er.RegistryEntry,
    statistics_by_id: dict[str, dict[str, Any]],
) -> EnvoyHistoryCandidate | None:
    entity_id = getattr(reg_entry, "entity_id", None)
    if not isinstance(entity_id, str) or not entity_id:
        return None

    state = hass.states.get(entity_id)
    if _is_compatible_energy_total_state(state):
        return EnvoyHistoryCandidate(
            entity_id=entity_id,
            config_entry_id=getattr(reg_entry, "config_entry_id", None),
            platform=getattr(reg_entry, "platform", None),
            title=_friendly_title(state, entity_id),
            current_value_kwh=_state_value_kwh(state) or 0.0,
        )

    metadata = statistics_by_id.get(entity_id)
    if not metadata or not metadata.get("has_sum"):
        return None
    current_value_kwh = await _last_statistic_value_kwh(
        hass,
        entity_id,
        _statistics_unit(metadata),
    )
    if current_value_kwh is None:
        return None
    return EnvoyHistoryCandidate(
        entity_id=entity_id,
        config_entry_id=getattr(reg_entry, "config_entry_id", None),
        platform=getattr(reg_entry, "platform", None),
        title=_friendly_title_from_name(metadata.get("name"), entity_id),
        current_value_kwh=current_value_kwh,
    )


async def discover_envoy_sources(hass: HomeAssistant) -> list[EnvoyHistorySource]:
    ent_reg = er.async_get(hass)
    registry_entries: list[er.RegistryEntry] = []
    statistic_ids: set[str] = set()
    for reg_entry in getattr(ent_reg, "entities", {}).values():
        if getattr(reg_entry, "domain", None) != "sensor":
            continue
        if getattr(reg_entry, "platform", None) != ENVOY_DOMAIN:
            continue
        config_entry_id = getattr(reg_entry, "config_entry_id", None)
        entity_id = getattr(reg_entry, "entity_id", None)
        if not isinstance(config_entry_id, str) or not entity_id:
            continue
        registry_entries.append(reg_entry)
        statistic_ids.add(entity_id)

    statistics_by_id = await _statistics_metadata_by_id(hass, statistic_ids)
    grouped: dict[str, list[EnvoyHistoryCandidate]] = {}
    for reg_entry in registry_entries:
        candidate = await _candidate_from_registry_entry(
            hass,
            reg_entry,
            statistics_by_id,
        )
        if candidate is None or candidate.config_entry_id is None:
            continue
        grouped.setdefault(candidate.config_entry_id, []).append(candidate)

    sources: list[EnvoyHistorySource] = []
    for entry_id, candidates in grouped.items():
        entry = hass.config_entries.async_get_entry(entry_id)
        title = entry.title if entry and entry.title else entry_id
        sources.append(
            EnvoyHistorySource(
                entry_id=entry_id,
                title=title,
                candidates=sorted(candidates, key=lambda item: item.title.lower()),
            )
        )
    return sorted(sources, key=lambda item: item.title.lower())


async def discover_external_migration_candidates(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
) -> list[EnvoyHistoryCandidate]:
    ent_reg = er.async_get(hass)
    registry_entries: list[er.RegistryEntry] = []
    statistic_ids: set[str] = set()
    for reg_entry in getattr(ent_reg, "entities", {}).values():
        if getattr(reg_entry, "domain", None) != "sensor":
            continue
        if getattr(reg_entry, "disabled_by", None) is not None:
            continue
        if getattr(reg_entry, "platform", None) in (ENVOY_DOMAIN, DOMAIN):
            continue
        if getattr(reg_entry, "config_entry_id", None) == entry.entry_id:
            continue
        entity_id = getattr(reg_entry, "entity_id", None)
        if not isinstance(entity_id, str) or not entity_id:
            continue
        registry_entries.append(reg_entry)
        statistic_ids.add(entity_id)

    statistics_by_id = await _statistics_metadata_by_id(hass, statistic_ids)
    candidates: list[EnvoyHistoryCandidate] = []
    for reg_entry in registry_entries:
        candidate = await _candidate_from_registry_entry(
            hass,
            reg_entry,
            statistics_by_id,
        )
        if candidate is not None:
            candidates.append(candidate)
    return sorted(candidates, key=lambda item: item.title.lower())


def discover_enphase_targets(
    hass: HomeAssistant, entry: EnphaseConfigEntry
) -> dict[str, EnvoyHistoryTarget]:
    site_id = str(entry.data.get(CONF_SITE_ID, "")).strip()
    if not site_id:
        return {}
    ent_reg = er.async_get(hass)
    targets: dict[str, EnvoyHistoryTarget] = {}
    for flow_key in MIGRATION_FLOWS:
        unique_id = migration_target_unique_id(site_id, flow_key)
        entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is None:
            continue
        reg_entry = ent_reg.async_get(entity_id)
        if reg_entry is None:
            continue
        if getattr(reg_entry, "config_entry_id", None) != entry.entry_id:
            continue
        if getattr(reg_entry, "disabled_by", None) is not None:
            continue
        state = hass.states.get(entity_id)
        targets[flow_key] = EnvoyHistoryTarget(
            flow_key=flow_key,
            label=FLOW_LABELS[flow_key],
            unique_id=unique_id,
            entity_id=entity_id,
            current_value_kwh=_state_value_kwh(state),
        )
    return targets


def _score_candidate(flow_key: str, candidate: EnvoyHistoryCandidate) -> int:
    object_id = _normalize_entity_tokens(candidate.entity_id)
    title_tokens = _normalize_title_tokens(candidate.title)
    combined_tokens = f"{object_id}_{title_tokens}".strip("_")
    score = 0
    if any(term in combined_tokens for term in _EXCLUDED_OBJECT_ID_TERMS):
        score -= 100
    if _PHASE_OBJECT_ID_PATTERN.search(object_id):
        score -= 120
    if flow_key == "solar_production":
        if "lifetime_production" in combined_tokens:
            score += 300
        if object_id.endswith("lifetime_production"):
            score += 120
        if "lifetime_pv" in combined_tokens or "pv_lifetime" in combined_tokens:
            score += 260
        if "production" in combined_tokens or "pv" in title_tokens:
            score += 80
        if "lifetime" in combined_tokens:
            score += 40
        if "consumption" in combined_tokens:
            score -= 160
    elif flow_key == "consumption":
        if "lifetime_consumption" in combined_tokens:
            score += 300
        if object_id.endswith("lifetime_consumption"):
            score += 120
        if "lifetime_load" in combined_tokens or "load_lifetime" in combined_tokens:
            score += 260
        if "consumption" in combined_tokens or "load" in title_tokens:
            score += 80
        if "lifetime" in combined_tokens:
            score += 40
        if "production" in combined_tokens or "pv" in title_tokens:
            score -= 160
    elif flow_key == "grid_import":
        if "grid_import" in combined_tokens:
            score += 300
        if "net_consumption" in combined_tokens:
            score += 200
        if object_id.endswith("lifetime_net_consumption"):
            score += 120
        if "energy_delivered" in combined_tokens or "delivered" in title_tokens:
            score += 180
        if "import" in combined_tokens:
            score += 120
        if "export" in combined_tokens:
            score -= 140
    elif flow_key == "grid_export":
        if "grid_export" in combined_tokens:
            score += 300
        if "net_production" in combined_tokens:
            score += 200
        if object_id.endswith("lifetime_net_production"):
            score += 120
        if "energy_received" in combined_tokens or "received" in title_tokens:
            score += 180
        if "export" in combined_tokens:
            score += 120
        if "import" in combined_tokens:
            score -= 140
    elif flow_key == "battery_charge":
        if "battery_charge" in combined_tokens:
            score += 300
        if object_id.endswith("lifetime_battery_charged"):
            score += 120
        if "energy_charged" in combined_tokens:
            score += 220
        if "charged" in combined_tokens:
            score += 140
        if "discharge" in combined_tokens:
            score -= 150
    elif flow_key == "battery_discharge":
        if "battery_discharge" in combined_tokens:
            score += 300
        if object_id.endswith("lifetime_battery_discharged"):
            score += 120
        if "energy_discharged" in combined_tokens:
            score += 220
        if "discharged" in combined_tokens:
            score += 140
        if "charge" in combined_tokens and "discharge" not in combined_tokens:
            score -= 150
    return score


def suggest_mappings(
    source: EnvoyHistorySource,
    targets: dict[str, EnvoyHistoryTarget],
    extra_candidates: list[EnvoyHistoryCandidate] | None = None,
) -> dict[str, str]:
    suggestions: dict[str, str] = {}
    used: set[str] = set()
    candidates = selection_candidates(source, extra_candidates)
    for flow_key in MIGRATION_FLOWS:
        if flow_key not in targets:
            continue
        scored = sorted(
            (
                (_score_candidate(flow_key, candidate), candidate)
                for candidate in candidates
                if candidate.entity_id not in used
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if not scored:
            continue
        best_score, best = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else None
        if best_score < 100:
            continue
        if second_score is not None and second_score == best_score:
            continue
        suggestions[flow_key] = best.entity_id
        used.add(best.entity_id)
    return suggestions


def selection_candidates(
    source: EnvoyHistorySource,
    extra_candidates: list[EnvoyHistoryCandidate] | None = None,
) -> list[EnvoyHistoryCandidate]:
    combined: list[EnvoyHistoryCandidate] = []
    seen: set[str] = set()
    for candidate in [*source.candidates, *(extra_candidates or [])]:
        if candidate.entity_id in seen:
            continue
        seen.add(candidate.entity_id)
        combined.append(candidate)
    return combined


def source_options(sources: list[EnvoyHistorySource]) -> list[dict[str, str]]:
    return [{"value": source.entry_id, "label": source.title} for source in sources]


def source_by_entry_id(
    sources: list[EnvoyHistorySource], entry_id: str | None
) -> EnvoyHistorySource | None:
    if entry_id is None:
        return None
    for source in sources:
        if source.entry_id == entry_id:
            return source
    return None


def candidate_options(
    source: EnvoyHistorySource,
    extra_candidates: list[EnvoyHistoryCandidate] | None = None,
) -> list[dict[str, str]]:
    options = [{"value": _OPTION_SKIP, "label": ""}]
    options.extend(
        {"value": candidate.entity_id, "label": candidate.title}
        for candidate in selection_candidates(source, extra_candidates)
    )
    return options


def selection_uses_source(
    source: EnvoyHistorySource,
    selected: dict[str, str],
    extra_candidates: list[EnvoyHistoryCandidate] | None = None,
) -> bool:
    candidate_lookup = {
        candidate.entity_id: candidate
        for candidate in selection_candidates(source, extra_candidates)
    }
    return any(
        candidate_lookup.get(entity_id) is not None
        and candidate_lookup[entity_id].config_entry_id == source.entry_id
        for entity_id in selected.values()
    )


def selected_mappings(
    user_input: dict[str, Any] | None,
) -> dict[str, str]:
    if not isinstance(user_input, dict):
        return {}
    selected: dict[str, str] = {}
    for flow_key in MIGRATION_FLOWS:
        value = user_input.get(flow_key)
        if not value or value == _OPTION_SKIP:
            continue
        selected[flow_key] = str(value)
    return selected


def validate_selected_mappings(
    hass: HomeAssistant,
    entry: EnphaseConfigEntry,
    source: EnvoyHistorySource,
    targets: dict[str, EnvoyHistoryTarget],
    selected: dict[str, str],
    extra_candidates: list[EnvoyHistoryCandidate] | None = None,
    *,
    require_source_unloaded: bool = True,
) -> EnvoyHistoryValidation:
    if not selected:
        return EnvoyHistoryValidation("migration_no_selection", [])

    selected_entity_ids = list(selected.values())
    if len(selected_entity_ids) != len(set(selected_entity_ids)):
        return EnvoyHistoryValidation("migration_duplicate_selection", [])

    candidate_lookup = {
        candidate.entity_id: candidate
        for candidate in selection_candidates(source, extra_candidates)
    }
    selected_from_source = selection_uses_source(source, selected, extra_candidates)
    source_entry = hass.config_entries.async_get_entry(source.entry_id)
    if (
        require_source_unloaded
        and selected_from_source
        and source_entry is not None
        and source_entry.state is ConfigEntryState.LOADED
    ):
        return EnvoyHistoryValidation("envoy_entry_loaded", [])

    ent_reg = er.async_get(hass)
    mappings: list[EnvoyHistoryMapping] = []
    warnings: list[EnvoyHistoryWarning] = []
    reserved_entity_ids = {
        getattr(reg_entry, "entity_id", "")
        for reg_entry in getattr(ent_reg, "entities", {}).values()
        if getattr(reg_entry, "entity_id", None)
    }
    for flow_key, old_entity_id in selected.items():
        target = targets.get(flow_key)
        candidate = candidate_lookup.get(old_entity_id)
        reg_entry = ent_reg.async_get(old_entity_id)
        if target is None or candidate is None or reg_entry is None:
            return EnvoyHistoryValidation("incompatible_energy_total", [])
        if (
            getattr(reg_entry, "platform", None) == DOMAIN
            or getattr(reg_entry, "config_entry_id", None) == entry.entry_id
        ):
            return EnvoyHistoryValidation("incompatible_energy_total", [])

        new_entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, target.unique_id)
        if new_entity_id is None:
            return EnvoyHistoryValidation("incompatible_energy_total", [])
        new_reg_entry = ent_reg.async_get(new_entity_id)
        if (
            new_reg_entry is None
            or getattr(new_reg_entry, "config_entry_id", None) != entry.entry_id
            or getattr(new_reg_entry, "platform", None) not in (None, DOMAIN)
        ):
            return EnvoyHistoryValidation("incompatible_energy_total", [])

        new_state = hass.states.get(new_entity_id)
        new_value_kwh = target.current_value_kwh
        if new_state is not None:
            if not _is_compatible_energy_total_state(new_state):
                return EnvoyHistoryValidation("incompatible_energy_total", [])
            new_value_kwh = _state_value_kwh(new_state)
        if new_value_kwh is None:
            return EnvoyHistoryValidation("incompatible_energy_total", [])
        archived_entity_id = _archive_entity_id(
            old_entity_id,
            reserved_entity_ids,
            getattr(reg_entry, "platform", None),
        )
        mapping = EnvoyHistoryMapping(
            flow_key=flow_key,
            label=target.label,
            old_entity_id=old_entity_id,
            archived_entity_id=archived_entity_id,
            old_value_kwh=candidate.current_value_kwh,
            new_entity_id=new_entity_id,
            new_value_kwh=new_value_kwh,
            target_unique_id=target.unique_id,
        )
        if new_value_kwh + LOW_VALUE_TOLERANCE_KWH < candidate.current_value_kwh:
            warnings.append(
                EnvoyHistoryWarning(
                    flow_key=flow_key,
                    label=target.label,
                    old_entity_id=old_entity_id,
                    old_value_kwh=candidate.current_value_kwh,
                    new_entity_id=new_entity_id,
                    new_value_kwh=new_value_kwh,
                )
            )
        mappings.append(mapping)
    return EnvoyHistoryValidation(None, mappings, warnings)


def execute_takeover(
    hass: HomeAssistant,
    mappings: list[EnvoyHistoryMapping],
    *,
    disable_archived_entities: bool = True,
) -> EnvoyHistoryExecutionError | None:
    ent_reg = er.async_get(hass)
    completed: list[EnvoyHistoryMapping] = []
    for mapping in mappings:
        old_reg_entry = ent_reg.async_get(mapping.old_entity_id)
        if old_reg_entry is None:
            return EnvoyHistoryExecutionError(
                completed=completed,
                failed=mapping,
                reason="Source entity is no longer available.",
            )
        current_target_entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, mapping.target_unique_id
        )
        if current_target_entity_id is None:
            return EnvoyHistoryExecutionError(
                completed=completed,
                failed=mapping,
                reason="Enphase Energy target entity is no longer available.",
            )
        try:
            update_kwargs: dict[str, object] = {
                "new_entity_id": mapping.archived_entity_id,
            }
            if disable_archived_entities:
                update_kwargs["disabled_by"] = er.RegistryEntryDisabler.USER
            ent_reg.async_update_entity(mapping.old_entity_id, **update_kwargs)
            if hass.states.get(mapping.old_entity_id) is not None:
                hass.states.async_remove(mapping.old_entity_id)
            ent_reg.async_update_entity(
                current_target_entity_id,
                new_entity_id=mapping.old_entity_id,
            )
        except Exception as err:  # noqa: BLE001
            return EnvoyHistoryExecutionError(
                completed=completed,
                failed=mapping,
                reason=str(err),
            )
        completed.append(mapping)
    return None


def format_mapping_preview(mappings: list[EnvoyHistoryMapping]) -> str:
    lines: list[str] = []
    for mapping in mappings:
        lines.append(
            f"- Archive source entity: `{mapping.old_entity_id}` -> "
            f"`{mapping.archived_entity_id}`"
        )
        lines.append(
            f"- Reassign Enphase Energy: `{mapping.new_entity_id}` -> "
            f"`{mapping.old_entity_id}`"
        )
    return "\n".join(lines)


def format_completed_preview(mappings: list[EnvoyHistoryMapping]) -> str:
    if not mappings:
        return "No mappings were completed."
    return format_mapping_preview(mappings)


def format_warning_preview(warnings: list[EnvoyHistoryWarning]) -> str:
    if not warnings:
        return ""
    lines = [
        "",
        "",
        "Warning: selected Enphase Energy totals are currently lower than the "
        "existing source totals. Migration can still continue.",
    ]
    for warning in warnings:
        lines.append(
            f"- `{warning.label}`: Enphase Energy `{warning.new_entity_id}` = "
            f"{warning.new_value_kwh:.2f} kWh; existing `{warning.old_entity_id}` = "
            f"{warning.old_value_kwh:.2f} kWh"
        )
    return "\n".join(lines)


def format_selection_preview(
    selected: dict[str, str], targets: dict[str, EnvoyHistoryTarget]
) -> str:
    lines: list[str] = []
    for flow_key in MIGRATION_FLOWS:
        old_entity_id = selected.get(flow_key)
        target = targets.get(flow_key)
        if not old_entity_id or target is None:
            continue
        lines.append(
            f"- Reassign Enphase Energy: `{target.entity_id}` -> `{old_entity_id}`"
        )
    return "\n".join(lines)
