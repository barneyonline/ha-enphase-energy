from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant import config_entries
from homeassistant.const import UnitOfEnergy
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enphase_ev.const import CONF_SITE_ID, DOMAIN
from custom_components.enphase_ev.envoy_history import (
    EnvoyHistoryCandidate,
    EnvoyHistoryMapping,
    EnvoyHistorySource,
    EnvoyHistoryTarget,
    EnvoyHistoryWarning,
    _archive_entity_id,
    _candidate_from_registry_entry,
    _friendly_title_from_name,
    _is_compatible_energy_total_state,
    _last_statistic_value_kwh,
    _normalize_text,
    _score_candidate,
    _state_value_kwh,
    _statistics_metadata_by_id,
    _unit_to_kwh_factor,
    candidate_options,
    discover_external_migration_candidates,
    discover_enphase_targets,
    discover_envoy_sources,
    execute_takeover,
    format_completed_preview,
    format_mapping_preview,
    format_selection_preview,
    format_warning_preview,
    migration_target_unique_id,
    selected_mappings,
    selection_candidates,
    skip_option_value,
    source_by_entry_id,
    source_options,
    suggest_mappings,
    validate_selected_mappings,
)


def _add_entity(
    hass,
    *,
    entry: MockConfigEntry,
    platform: str,
    unique_id: str,
    object_id: str,
    state: str | None,
    attrs: dict[str, object] | None = None,
    disabled_by=None,
) -> str:
    ent_reg = er.async_get(hass)
    reg_entry = ent_reg.async_get_or_create(
        "sensor",
        platform,
        unique_id,
        config_entry=entry,
        suggested_object_id=object_id,
        disabled_by=disabled_by,
    )
    if state is not None:
        hass.states.async_set(reg_entry.entity_id, state, attrs or {})
    return reg_entry.entity_id


def _energy_attrs(
    *,
    unit: str = UnitOfEnergy.KILO_WATT_HOUR,
    state_class: str = "total_increasing",
    friendly_name: str | None = None,
) -> dict[str, object]:
    attrs: dict[str, object] = {
        "device_class": "energy",
        "state_class": state_class,
        "unit_of_measurement": unit,
    }
    if friendly_name is not None:
        attrs["friendly_name"] = friendly_name
    return attrs


def _patch_entry_lookup(monkeypatch, hass, *entries: MockConfigEntry) -> None:
    original = hass.config_entries.async_get_entry
    lookup = {entry.entry_id: entry for entry in entries}
    monkeypatch.setattr(
        hass.config_entries,
        "async_get_entry",
        lambda entry_id: lookup.get(entry_id) or original(entry_id),
    )


class _BadStr:
    def __str__(self) -> str:
        raise RuntimeError("bad")


def test_helper_functions_handle_invalid_values(hass, monkeypatch) -> None:
    assert EnvoyHistorySource("envoy-entry", "Envoy", []).candidate_by_entity_id() == {}
    assert _archive_entity_id("invalid", set(), None) == "invalid"
    assert _normalize_text(_BadStr()) == ""
    assert _unit_to_kwh_factor("") is None
    assert _state_value_kwh(None) is None
    assert (
        _state_value_kwh(
            State(
                "sensor.invalid_energy",
                "bad",
                {
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                },
            )
        )
        is None
    )
    assert (
        _state_value_kwh(
            State(
                "sensor.negative_energy",
                "-1",
                {"unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR},
            )
        )
        is None
    )
    assert not _is_compatible_energy_total_state(
        State(
            "sensor.bad_state_class",
            "1",
            {
                "device_class": "energy",
                "state_class": "measurement",
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            },
        )
    )
    assert _friendly_title_from_name(None, "sensor.x") == "sensor.x"


@pytest.mark.asyncio
async def test_statistics_helpers_cover_error_paths(hass, monkeypatch) -> None:
    assert await _statistics_metadata_by_id(hass, set()) == {}
    monkeypatch.setattr(
        "custom_components.enphase_ev.envoy_history.recorder_statistics.async_list_statistic_ids",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    assert await _statistics_metadata_by_id(hass, {"sensor.x"}) == {}

    assert await _last_statistic_value_kwh(hass, "sensor.x", "unknown_unit") is None

    monkeypatch.setattr(
        "custom_components.enphase_ev.envoy_history.recorder_statistics.get_last_statistics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert (
        await _last_statistic_value_kwh(hass, "sensor.x", UnitOfEnergy.KILO_WATT_HOUR)
        is None
    )

    monkeypatch.setattr(
        "custom_components.enphase_ev.envoy_history.recorder_statistics.get_last_statistics",
        lambda *_args, **_kwargs: {"sensor.x": []},
    )
    assert (
        await _last_statistic_value_kwh(hass, "sensor.x", UnitOfEnergy.KILO_WATT_HOUR)
        is None
    )

    monkeypatch.setattr(
        "custom_components.enphase_ev.envoy_history.recorder_statistics.get_last_statistics",
        lambda *_args, **_kwargs: {"sensor.x": [{"sum": "bad"}]},
    )
    assert (
        await _last_statistic_value_kwh(hass, "sensor.x", UnitOfEnergy.KILO_WATT_HOUR)
        is None
    )

    monkeypatch.setattr(
        "custom_components.enphase_ev.envoy_history.recorder_statistics.get_last_statistics",
        lambda *_args, **_kwargs: {"sensor.x": [{"state": -1}]},
    )
    assert (
        await _last_statistic_value_kwh(hass, "sensor.x", UnitOfEnergy.KILO_WATT_HOUR)
        is None
    )


@pytest.mark.asyncio
async def test_candidate_from_registry_entry_filters_invalid_registry_and_stats(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(domain="template", entry_id="template-entry")
    entry.add_to_hass(hass)
    reg_entry = er.async_get(hass).async_get_or_create(
        "sensor",
        "template",
        "template_total",
        config_entry=entry,
        suggested_object_id="template_total",
    )
    assert await _candidate_from_registry_entry(hass, reg_entry, {}) is None

    monkeypatch.setattr(
        "custom_components.enphase_ev.envoy_history._last_statistic_value_kwh",
        AsyncMock(return_value=None),
    )
    assert (
        await _candidate_from_registry_entry(
            hass,
            reg_entry,
            {
                reg_entry.entity_id: {
                    "has_sum": True,
                    "name": "Template Total",
                    "statistics_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                }
            },
        )
        is None
    )
    assert await _candidate_from_registry_entry(hass, object(), {}) is None


@pytest.mark.asyncio
async def test_discover_envoy_sources_groups_compatible_entities(
    hass, monkeypatch
) -> None:
    envoy_a = MockConfigEntry(domain="enphase_envoy", title="Envoy A")
    envoy_b = MockConfigEntry(domain="enphase_envoy", title="Envoy B")
    _patch_entry_lookup(monkeypatch, hass, envoy_a, envoy_b)

    _add_entity(
        hass,
        entry=envoy_a,
        platform="enphase_envoy",
        unique_id="envoy_a_prod",
        object_id="envoy_lifetime_production",
        state="1234",
        attrs=_energy_attrs(unit=UnitOfEnergy.WATT_HOUR, friendly_name="Lifetime PV"),
    )
    _add_entity(
        hass,
        entry=envoy_a,
        platform="enphase_envoy",
        unique_id="envoy_a_power",
        object_id="envoy_current_power",
        state="200",
        attrs={
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
        },
    )
    _add_entity(
        hass,
        entry=envoy_b,
        platform="enphase_envoy",
        unique_id="envoy_b_cons",
        object_id="envoy_lifetime_consumption",
        state="4.5",
        attrs=_energy_attrs(friendly_name="Lifetime Load"),
    )
    template_entry = MockConfigEntry(domain="template", title="Template")
    _patch_entry_lookup(monkeypatch, hass, envoy_a, envoy_b, template_entry)
    _add_entity(
        hass,
        entry=template_entry,
        platform="template",
        unique_id="template_prod",
        object_id="template_lifetime_production",
        state="6.0",
        attrs=_energy_attrs(friendly_name="Template Lifetime Production"),
    )

    sources = await discover_envoy_sources(hass)

    assert [source.title for source in sources] == ["Envoy A", "Envoy B"]
    assert [candidate.entity_id for candidate in sources[0].candidates] == [
        "sensor.envoy_lifetime_production"
    ]
    assert sources[0].candidates[0].current_value_kwh == pytest.approx(1.234)


@pytest.mark.asyncio
async def test_discover_external_candidates_filters_registry_entries(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    entry.add_to_hass(hass)
    template_entry = MockConfigEntry(domain="template", entry_id="template-entry")
    _patch_entry_lookup(monkeypatch, hass, template_entry)

    _add_entity(
        hass,
        entry=template_entry,
        platform="template",
        unique_id="template_total",
        object_id="template_lifetime_production",
        state="1",
        attrs=_energy_attrs(),
    )
    _add_entity(
        hass,
        entry=template_entry,
        platform="template",
        unique_id="disabled_total",
        object_id="template_disabled_total",
        state="1",
        attrs=_energy_attrs(),
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    candidates = await discover_external_migration_candidates(hass, entry)

    assert [candidate.entity_id for candidate in candidates] == [
        "sensor.template_lifetime_production"
    ]


@pytest.mark.asyncio
async def test_discover_envoy_sources_uses_recorder_stats_when_unloaded(
    hass, monkeypatch
) -> None:
    envoy = MockConfigEntry(domain="enphase_envoy", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)

    entity_id = _add_entity(
        hass,
        entry=envoy,
        platform="enphase_envoy",
        unique_id="envoy_a_prod",
        object_id="envoy_lifetime_production",
        state=None,
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.envoy_history.recorder_statistics.async_list_statistic_ids",
        AsyncMock(
            return_value=[
                {
                    "statistic_id": entity_id,
                    "has_sum": True,
                    "name": "Lifetime PV",
                    "statistics_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "custom_components.enphase_ev.envoy_history.recorder_statistics.get_last_statistics",
        lambda *_args, **_kwargs: {entity_id: [{"sum": 5.25}]},
    )

    sources = await discover_envoy_sources(hass)

    assert [source.title for source in sources] == ["Envoy A"]
    assert [candidate.entity_id for candidate in sources[0].candidates] == [entity_id]
    assert sources[0].candidates[0].title == f"Lifetime PV ({entity_id})"
    assert sources[0].candidates[0].current_value_kwh == pytest.approx(5.25)


@pytest.mark.asyncio
async def test_discover_envoy_sources_and_external_candidates_skip_invalid_registry_objects(
    hass, monkeypatch
) -> None:
    envoy = MockConfigEntry(domain="enphase_envoy", title="Envoy A")
    _patch_entry_lookup(monkeypatch, hass, envoy)
    ent_reg = er.async_get(hass)
    original_entities = ent_reg.entities

    class _Fake:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake_entities = {
        "bad_domain": _Fake(
            domain="switch",
            platform="enphase_envoy",
            config_entry_id="x",
            entity_id="switch.x",
        ),
        "missing_id": _Fake(
            domain="sensor",
            platform="enphase_envoy",
            config_entry_id="x",
            entity_id=None,
        ),
        "missing_entry": _Fake(
            domain="sensor",
            platform="enphase_envoy",
            config_entry_id=None,
            entity_id="sensor.x",
        ),
        "external_bad_domain": _Fake(
            domain="switch",
            platform="template",
            config_entry_id="t",
            entity_id="switch.x",
        ),
        "external_same_entry": _Fake(
            domain="sensor",
            platform="template",
            config_entry_id="enphase-entry",
            entity_id="sensor.same",
        ),
        "external_missing_id": _Fake(
            domain="sensor", platform="template", config_entry_id="t", entity_id=None
        ),
    }
    monkeypatch.setattr(ent_reg, "entities", fake_entities)

    assert await discover_envoy_sources(hass) == []
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    assert await discover_external_migration_candidates(hass, entry) == []
    monkeypatch.setattr(ent_reg, "entities", original_entities)


def test_discover_enphase_targets_uses_unique_ids_for_entry(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    entry.add_to_hass(hass)
    other_entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "12345"})
    other_entry.add_to_hass(hass)

    _add_entity(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="3.5",
        attrs=_energy_attrs(),
    )
    _add_entity(
        hass,
        entry=other_entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "grid_import"),
        object_id="site_grid_import",
        state="2.0",
        attrs=_energy_attrs(),
    )

    targets = discover_enphase_targets(hass, entry)

    assert list(targets) == ["solar_production"]
    assert targets["solar_production"].entity_id == "sensor.site_solar_production"
    assert targets["solar_production"].current_value_kwh == pytest.approx(3.5)


def test_discover_enphase_targets_skips_missing_and_disabled_registry_entries(
    hass,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    disabled_id = _add_entity(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "grid_import"),
        object_id="site_grid_import",
        state="1",
        attrs=_energy_attrs(),
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get(disabled_id) is not None
    assert discover_enphase_targets(hass, entry) == {}


def test_discover_enphase_targets_skips_missing_registry_entry_lookup(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="entry", data={CONF_SITE_ID: "12345"}
    )
    entry.add_to_hass(hass)
    _add_entity(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("12345", "solar_production"),
        object_id="site_solar_production",
        state="1",
        attrs=_energy_attrs(),
    )
    ent_reg = er.async_get(hass)
    original_async_get = ent_reg.async_get

    def _async_get(entity_id: str):
        if entity_id == "sensor.site_solar_production":
            return None
        return original_async_get(entity_id)

    monkeypatch.setattr(ent_reg, "async_get", _async_get)

    assert discover_enphase_targets(hass, entry) == {}


def test_suggest_mappings_prefers_high_confidence_candidates() -> None:
    source = EnvoyHistorySource(
        entry_id="envoy-entry",
        title="Envoy",
        candidates=[
            EnvoyHistoryCandidate(
                entity_id="sensor.envoy_lifetime_production",
                config_entry_id="envoy-entry",
                title="Production",
                current_value_kwh=10,
            ),
            EnvoyHistoryCandidate(
                entity_id="sensor.envoy_lifetime_consumption",
                config_entry_id="envoy-entry",
                title="Consumption",
                current_value_kwh=12,
            ),
            EnvoyHistoryCandidate(
                entity_id="sensor.envoy_energy_delivered",
                config_entry_id="envoy-entry",
                title="Delivered",
                current_value_kwh=9,
            ),
        ],
    )
    targets = {
        "solar_production": EnvoyHistoryTarget(
            flow_key="solar_production",
            label="Site Solar Production",
            unique_id="uid-prod",
            entity_id="sensor.site_solar_production",
            current_value_kwh=10,
        ),
        "grid_import": EnvoyHistoryTarget(
            flow_key="grid_import",
            label="Site Grid Import",
            unique_id="uid-import",
            entity_id="sensor.site_grid_import",
            current_value_kwh=9,
        ),
    }

    suggestions = suggest_mappings(source, targets)

    assert suggestions == {
        "solar_production": "sensor.envoy_lifetime_production",
        "grid_import": "sensor.envoy_energy_delivered",
    }


def test_suggest_mappings_leaves_ambiguous_candidates_blank() -> None:
    source = EnvoyHistorySource(
        entry_id="envoy-entry",
        title="Envoy",
        candidates=[
            EnvoyHistoryCandidate(
                entity_id="sensor.grid_import_a",
                config_entry_id="envoy-entry",
                title="Import A",
                current_value_kwh=10,
            ),
            EnvoyHistoryCandidate(
                entity_id="sensor.grid_import_b",
                config_entry_id="envoy-entry",
                title="Import B",
                current_value_kwh=11,
            ),
        ],
    )
    targets = {
        "grid_import": EnvoyHistoryTarget(
            flow_key="grid_import",
            label="Site Grid Import",
            unique_id="uid-import",
            entity_id="sensor.site_grid_import",
            current_value_kwh=11,
        )
    }

    assert suggest_mappings(source, targets) == {}


def test_suggest_mappings_prefers_total_over_phase_specific_candidates() -> None:
    source = EnvoyHistorySource(
        entry_id="envoy-entry",
        title="Envoy",
        candidates=[
            EnvoyHistoryCandidate(
                entity_id="sensor.envoy_lifetime_production_l1",
                config_entry_id="envoy-entry",
                title="Production L1",
                current_value_kwh=3,
            ),
            EnvoyHistoryCandidate(
                entity_id="sensor.envoy_lifetime_production_l2",
                config_entry_id="envoy-entry",
                title="Production L2",
                current_value_kwh=3,
            ),
            EnvoyHistoryCandidate(
                entity_id="sensor.envoy_lifetime_production",
                config_entry_id="envoy-entry",
                title="Production Total",
                current_value_kwh=6,
            ),
        ],
    )
    targets = {
        "solar_production": EnvoyHistoryTarget(
            flow_key="solar_production",
            label="Site Solar Production",
            unique_id="uid-prod",
            entity_id="sensor.site_solar_production",
            current_value_kwh=6,
        )
    }

    assert suggest_mappings(source, targets) == {
        "solar_production": "sensor.envoy_lifetime_production"
    }


def test_suggest_mappings_uses_friendly_titles_for_lifetime_matches() -> None:
    source = EnvoyHistorySource(
        entry_id="envoy-entry",
        title="Envoy",
        candidates=[
            EnvoyHistoryCandidate(
                entity_id="sensor.envoy_meter_001",
                config_entry_id="envoy-entry",
                title="Lifetime PV (sensor.envoy_meter_001)",
                current_value_kwh=6,
            ),
            EnvoyHistoryCandidate(
                entity_id="sensor.envoy_meter_002",
                config_entry_id="envoy-entry",
                title="Lifetime Load (sensor.envoy_meter_002)",
                current_value_kwh=7,
            ),
        ],
    )
    targets = {
        "solar_production": EnvoyHistoryTarget(
            flow_key="solar_production",
            label="Site Solar Production",
            unique_id="uid-prod",
            entity_id="sensor.site_solar_production",
            current_value_kwh=6,
        ),
        "consumption": EnvoyHistoryTarget(
            flow_key="consumption",
            label="Site Consumption",
            unique_id="uid-cons",
            entity_id="sensor.site_consumption",
            current_value_kwh=7,
        ),
    }

    assert suggest_mappings(source, targets) == {
        "solar_production": "sensor.envoy_meter_001",
        "consumption": "sensor.envoy_meter_002",
    }


@pytest.mark.parametrize(
    ("flow_key", "entity_id", "title", "expected_sign"),
    [
        ("grid_import", "sensor.grid_export", "Grid Export", -1),
        ("grid_export", "sensor.energy_import", "Import", -1),
        ("battery_charge", "sensor.battery_discharge", "Battery Discharge", -1),
        ("battery_discharge", "sensor.battery_charge", "Battery Charge", -1),
        ("consumption", "sensor.production_total", "PV", -1),
        ("grid_import", "sensor.lifetime_net_consumption", "Net Consumption", 1),
        ("grid_export", "sensor.lifetime_net_production", "Net Production", 1),
    ],
)
def test_score_candidate_handles_branch_specific_terms(
    flow_key: str, entity_id: str, title: str, expected_sign: int
) -> None:
    score = _score_candidate(
        flow_key,
        EnvoyHistoryCandidate(
            entity_id=entity_id,
            config_entry_id="entry",
            title=title,
            current_value_kwh=1.0,
        ),
    )
    assert score * expected_sign > 0


def test_suggest_mappings_skips_low_confidence_candidates() -> None:
    source = EnvoyHistorySource(
        entry_id="envoy-entry",
        title="Envoy",
        candidates=[
            EnvoyHistoryCandidate(
                entity_id="sensor.unrelated",
                config_entry_id="envoy-entry",
                title="Other",
                current_value_kwh=1.0,
            )
        ],
    )
    targets = {
        "grid_import": EnvoyHistoryTarget(
            flow_key="grid_import",
            label="Grid Import",
            unique_id="uid",
            entity_id="sensor.site_grid_import",
            current_value_kwh=1.0,
        )
    }

    assert suggest_mappings(source, targets) == {}


def test_suggest_mappings_allows_candidates_when_target_total_is_lower() -> None:
    source = EnvoyHistorySource(
        entry_id="envoy-entry",
        title="Envoy",
        candidates=[
            EnvoyHistoryCandidate(
                entity_id="sensor.envoy_lifetime_production",
                config_entry_id="envoy-entry",
                title="Production",
                current_value_kwh=10.0,
            )
        ],
    )
    targets = {
        "solar_production": EnvoyHistoryTarget(
            flow_key="solar_production",
            label="Solar",
            unique_id="uid-prod",
            entity_id="sensor.site_solar_production",
            current_value_kwh=9.5,
        )
    }

    assert suggest_mappings(source, targets) == {
        "solar_production": "sensor.envoy_lifetime_production"
    }


def test_score_candidate_hits_additional_flow_branches() -> None:
    assert (
        _score_candidate(
            "battery_charge",
            EnvoyHistoryCandidate(
                "sensor.lifetime_battery_charged", "entry", "Charged", 1.0
            ),
        )
        > 0
    )
    assert (
        _score_candidate(
            "battery_discharge",
            EnvoyHistoryCandidate(
                "sensor.lifetime_battery_discharged", "entry", "Discharged", 1.0
            ),
        )
        > 0
    )


def test_selected_mappings_skips_empty_values() -> None:
    assert skip_option_value()
    assert selected_mappings(
        {
            "solar_production": "sensor.a",
            "grid_import": skip_option_value(),
            "grid_export": "",
        }
    ) == {"solar_production": "sensor.a"}


def test_source_helpers_render_options() -> None:
    source = EnvoyHistorySource(
        entry_id="envoy-entry",
        title="Envoy",
        candidates=[
            EnvoyHistoryCandidate(
                entity_id="sensor.envoy_lifetime_production",
                config_entry_id="envoy-entry",
                title="Production",
                current_value_kwh=10,
            )
        ],
    )
    extra = [
        EnvoyHistoryCandidate(
            entity_id="sensor.template_energy_total",
            config_entry_id="template-entry",
            platform="template",
            title="Template Energy Total (sensor.template_energy_total)",
            current_value_kwh=11,
        )
    ]

    assert source_by_entry_id([source], "envoy-entry") is source
    assert source_options([source]) == [{"value": "envoy-entry", "label": "Envoy"}]
    assert candidate_options(source)[0]["value"] == skip_option_value()
    assert candidate_options(source)[0]["label"] == ""
    assert [option["value"] for option in candidate_options(source, extra)] == [
        skip_option_value(),
        "sensor.envoy_lifetime_production",
        "sensor.template_energy_total",
    ]
    assert source_by_entry_id([source], None) is None
    assert source_by_entry_id([source], "missing") is None
    assert selected_mappings(None) == {}


def test_additional_helper_branches(hass) -> None:
    assert (
        _state_value_kwh(State("sensor.missing_unit", "1", {"device_class": "energy"}))
        is None
    )
    assert discover_enphase_targets(hass, MockConfigEntry(domain=DOMAIN, data={})) == {}
    assert selection_candidates(
        EnvoyHistorySource(
            "envoy",
            "Envoy",
            [EnvoyHistoryCandidate("sensor.same", "entry", "Same", 1.0)],
        ),
        [EnvoyHistoryCandidate("sensor.same", "entry", "Same", 1.0)],
    ) == [EnvoyHistoryCandidate("sensor.same", "entry", "Same", 1.0)]
    assert (
        _score_candidate(
            "solar_production",
            EnvoyHistoryCandidate(
                "sensor.current_power", "entry", "Current Power", 1.0
            ),
        )
        < 0
    )
    assert (
        _score_candidate(
            "grid_export",
            EnvoyHistoryCandidate(
                "sensor.grid_export_energy_received", "entry", "Received Export", 1.0
            ),
        )
        > 0
    )
    assert (
        _score_candidate(
            "battery_charge",
            EnvoyHistoryCandidate(
                "sensor.lifetime_battery_charged_energy_charged",
                "entry",
                "Charged",
                1.0,
            ),
        )
        > 0
    )
    assert (
        _score_candidate(
            "battery_discharge",
            EnvoyHistoryCandidate(
                "sensor.lifetime_battery_discharged_energy_discharged",
                "entry",
                "Discharged",
                1.0,
            ),
        )
        > 0
    )


def test_validate_selected_mappings_rejects_duplicates(hass, monkeypatch) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    entry.add_to_hass(hass)
    source_entry = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-entry")
    _patch_entry_lookup(monkeypatch, hass, source_entry)

    source = EnvoyHistorySource(
        entry_id="envoy-entry",
        title="Envoy",
        candidates=[
            EnvoyHistoryCandidate(
                entity_id="sensor.same",
                config_entry_id="envoy-entry",
                title="Same",
                current_value_kwh=1.0,
            )
        ],
    )
    targets = {
        "solar_production": EnvoyHistoryTarget(
            flow_key="solar_production",
            label="Solar",
            unique_id="uid-1",
            entity_id="sensor.one",
            current_value_kwh=1.0,
        ),
        "consumption": EnvoyHistoryTarget(
            flow_key="consumption",
            label="Consumption",
            unique_id="uid-2",
            entity_id="sensor.two",
            current_value_kwh=1.0,
        ),
    }

    result = validate_selected_mappings(
        hass,
        entry,
        source,
        targets,
        {"solar_production": "sensor.same", "consumption": "sensor.same"},
    )

    assert result.error == "migration_duplicate_selection"


def test_validate_selected_mappings_handles_incompatible_cases(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    entry.add_to_hass(hass)
    source_entry = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-entry")
    _patch_entry_lookup(monkeypatch, hass, source_entry)
    source = EnvoyHistorySource(
        "envoy-entry",
        "Envoy",
        [EnvoyHistoryCandidate("sensor.old", "envoy-entry", "Old", 1.0)],
    )
    target = EnvoyHistoryTarget(
        flow_key="solar_production",
        label="Solar",
        unique_id="uid",
        entity_id="sensor.site_solar_production",
        current_value_kwh=1.0,
    )
    assert (
        validate_selected_mappings(
            hass,
            entry,
            source,
            {"solar_production": target},
            {"solar_production": "sensor.old"},
        ).error
        == "incompatible_energy_total"
    )

    old_entity_id = _add_entity(
        hass,
        entry=source_entry,
        platform=DOMAIN,
        unique_id="old",
        object_id="old",
        state="1",
        attrs=_energy_attrs(),
    )
    assert (
        validate_selected_mappings(
            hass,
            entry,
            EnvoyHistorySource(
                "envoy-entry",
                "Envoy",
                [EnvoyHistoryCandidate(old_entity_id, "envoy-entry", "Old", 1.0)],
            ),
            {"solar_production": target},
            {"solar_production": old_entity_id},
            require_source_unloaded=False,
        ).error
        == "incompatible_energy_total"
    )

    old_entity_id = _add_entity(
        hass,
        entry=source_entry,
        platform="enphase_envoy",
        unique_id="real_old",
        object_id="real_old",
        state="1",
        attrs=_energy_attrs(),
    )
    assert (
        validate_selected_mappings(
            hass,
            entry,
            EnvoyHistorySource(
                "envoy-entry",
                "Envoy",
                [EnvoyHistoryCandidate(old_entity_id, "envoy-entry", "Old", 1.0)],
            ),
            {"solar_production": target},
            {"solar_production": old_entity_id},
            require_source_unloaded=False,
        ).error
        == "incompatible_energy_total"
    )


def test_validate_selected_mappings_requires_unloaded_envoy(hass, monkeypatch) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    entry.add_to_hass(hass)
    source_entry = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-entry")
    _patch_entry_lookup(monkeypatch, hass, source_entry)
    object.__setattr__(source_entry, "state", config_entries.ConfigEntryState.LOADED)

    result = validate_selected_mappings(
        hass,
        entry,
        EnvoyHistorySource(
            "envoy-entry",
            "Envoy",
            [
                EnvoyHistoryCandidate(
                    entity_id="sensor.old",
                    config_entry_id="envoy-entry",
                    title="Old",
                    current_value_kwh=1.0,
                )
            ],
        ),
        {},
        {"solar_production": "sensor.old"},
    )

    assert result.error == "envoy_entry_loaded"


def test_validate_selected_mappings_warns_for_lower_target_value(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    entry.add_to_hass(hass)
    source_entry = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-entry")
    _patch_entry_lookup(monkeypatch, hass, source_entry)

    old_entity_id = _add_entity(
        hass,
        entry=source_entry,
        platform="enphase_envoy",
        unique_id="envoy_old",
        object_id="envoy_lifetime_production",
        state="4.0",
        attrs=_energy_attrs(),
    )
    new_entity_id = _add_entity(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("1", "solar_production"),
        object_id="site_solar_production",
        state="3.0",
        attrs=_energy_attrs(),
    )

    result = validate_selected_mappings(
        hass,
        entry,
        EnvoyHistorySource(
            entry_id="envoy-entry",
            title="Envoy",
            candidates=[
                EnvoyHistoryCandidate(
                    entity_id=old_entity_id,
                    config_entry_id="envoy-entry",
                    title="Old",
                    current_value_kwh=4.0,
                )
            ],
        ),
        {
            "solar_production": EnvoyHistoryTarget(
                flow_key="solar_production",
                label="Solar",
                unique_id=migration_target_unique_id("1", "solar_production"),
                entity_id=new_entity_id,
                current_value_kwh=3.0,
            )
        },
        {"solar_production": old_entity_id},
        require_source_unloaded=False,
    )

    assert result.error is None
    assert result.mappings == [
        EnvoyHistoryMapping(
            flow_key="solar_production",
            label="Solar",
            old_entity_id=old_entity_id,
            archived_entity_id="sensor.envoy_lifetime_production_envoy_legacy",
            old_value_kwh=4.0,
            new_entity_id=new_entity_id,
            new_value_kwh=3.0,
            target_unique_id=migration_target_unique_id("1", "solar_production"),
        )
    ]
    assert result.warnings == [
        EnvoyHistoryWarning(
            flow_key="solar_production",
            label="Solar",
            old_entity_id=old_entity_id,
            old_value_kwh=4.0,
            new_entity_id=new_entity_id,
            new_value_kwh=3.0,
        )
    ]


def test_validate_selected_mappings_succeeds(hass, monkeypatch) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    entry.add_to_hass(hass)
    source_entry = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-entry")
    _patch_entry_lookup(monkeypatch, hass, source_entry)

    old_entity_id = _add_entity(
        hass,
        entry=source_entry,
        platform="enphase_envoy",
        unique_id="envoy_old",
        object_id="envoy_lifetime_production",
        state="4000",
        attrs=_energy_attrs(unit=UnitOfEnergy.WATT_HOUR),
    )
    new_entity_id = _add_entity(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("1", "solar_production"),
        object_id="site_solar_production",
        state="4.2",
        attrs=_energy_attrs(),
    )
    object.__setattr__(
        source_entry, "state", config_entries.ConfigEntryState.NOT_LOADED
    )

    result = validate_selected_mappings(
        hass,
        entry,
        EnvoyHistorySource(
            entry_id="envoy-entry",
            title="Envoy",
            candidates=[
                EnvoyHistoryCandidate(
                    entity_id=old_entity_id,
                    config_entry_id="envoy-entry",
                    title="Old",
                    current_value_kwh=4.0,
                )
            ],
        ),
        {
            "solar_production": EnvoyHistoryTarget(
                flow_key="solar_production",
                label="Solar",
                unique_id=migration_target_unique_id("1", "solar_production"),
                entity_id=new_entity_id,
                current_value_kwh=4.2,
            )
        },
        {"solar_production": old_entity_id},
    )

    assert result.error is None
    assert result.mappings == [
        EnvoyHistoryMapping(
            flow_key="solar_production",
            label="Solar",
            old_entity_id=old_entity_id,
            archived_entity_id="sensor.envoy_lifetime_production_envoy_legacy",
            old_value_kwh=4.0,
            new_entity_id=new_entity_id,
            new_value_kwh=4.2,
            target_unique_id=migration_target_unique_id("1", "solar_production"),
        )
    ]
    assert result.warnings == []


def test_format_warning_preview_lists_lower_value_mappings() -> None:
    assert format_warning_preview(
        [
            EnvoyHistoryWarning(
                flow_key="solar_production",
                label="Site Solar Production",
                old_entity_id="sensor.envoy_lifetime_production",
                old_value_kwh=5.0,
                new_entity_id="sensor.site_solar_production",
                new_value_kwh=4.0,
            )
        ]
    ) == (
        "\n\nWarning: selected Enphase Energy totals are currently lower than the "
        "existing source totals. Migration can still continue.\n"
        "- `Site Solar Production`: Enphase Energy `sensor.site_solar_production` "
        "= 4.00 kWh; existing `sensor.envoy_lifetime_production` = 5.00 kWh"
    )


def test_format_warning_preview_returns_empty_without_warnings() -> None:
    assert format_warning_preview([]) == ""


def test_validate_selected_mappings_allows_external_compatible_sensor(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    entry.add_to_hass(hass)
    source_entry = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-entry")
    template_entry = MockConfigEntry(domain="template", entry_id="template-entry")
    _patch_entry_lookup(monkeypatch, hass, source_entry, template_entry)

    external_entity_id = _add_entity(
        hass,
        entry=template_entry,
        platform="template",
        unique_id="template_total",
        object_id="template_lifetime_production",
        state="4.0",
        attrs=_energy_attrs(friendly_name="Template Lifetime Production"),
    )
    new_entity_id = _add_entity(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("1", "solar_production"),
        object_id="site_solar_production",
        state="4.2",
        attrs=_energy_attrs(),
    )
    object.__setattr__(source_entry, "state", config_entries.ConfigEntryState.LOADED)

    result = validate_selected_mappings(
        hass,
        entry,
        EnvoyHistorySource("envoy-entry", "Envoy", []),
        {
            "solar_production": EnvoyHistoryTarget(
                flow_key="solar_production",
                label="Solar",
                unique_id=migration_target_unique_id("1", "solar_production"),
                entity_id=new_entity_id,
                current_value_kwh=4.2,
            )
        },
        {"solar_production": external_entity_id},
        [
            EnvoyHistoryCandidate(
                entity_id=external_entity_id,
                config_entry_id="template-entry",
                platform="template",
                title="Template Lifetime Production",
                current_value_kwh=4.0,
            )
        ],
    )

    assert result.error is None
    assert result.mappings[0].old_entity_id == external_entity_id
    assert (
        result.mappings[0].archived_entity_id
        == "sensor.template_lifetime_production_legacy"
    )


def test_validate_selected_mappings_rejects_bad_target_registry_and_state(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    entry.add_to_hass(hass)
    source_entry = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-entry")
    _patch_entry_lookup(monkeypatch, hass, source_entry)

    old_entity_id = _add_entity(
        hass,
        entry=source_entry,
        platform="enphase_envoy",
        unique_id="envoy_old",
        object_id="envoy_lifetime_production",
        state="1.0",
        attrs=_energy_attrs(),
    )
    source = EnvoyHistorySource(
        "envoy-entry",
        "Envoy",
        [EnvoyHistoryCandidate(old_entity_id, "envoy-entry", "Old", 1.0)],
    )
    target_uid = migration_target_unique_id("1", "solar_production")
    assert (
        validate_selected_mappings(
            hass,
            entry,
            source,
            {
                "solar_production": EnvoyHistoryTarget(
                    flow_key="solar_production",
                    label="Solar",
                    unique_id=target_uid,
                    entity_id="sensor.site_solar_production",
                    current_value_kwh=1.1,
                )
            },
            {"solar_production": old_entity_id},
            require_source_unloaded=False,
        ).error
        == "incompatible_energy_total"
    )

    _add_entity(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=target_uid,
        object_id="site_solar_production",
        state="1.1",
        attrs={
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
        },
    )
    assert (
        validate_selected_mappings(
            hass,
            entry,
            source,
            {
                "solar_production": EnvoyHistoryTarget(
                    flow_key="solar_production",
                    label="Solar",
                    unique_id=target_uid,
                    entity_id="sensor.site_solar_production",
                    current_value_kwh=1.1,
                )
            },
            {"solar_production": old_entity_id},
            require_source_unloaded=False,
        ).error
        == "incompatible_energy_total"
    )

    ent_reg = er.async_get(hass)
    new_entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, target_uid)
    assert new_entity_id is not None
    new_reg_entry = ent_reg.async_get(new_entity_id)
    assert new_reg_entry is not None
    object.__setattr__(new_reg_entry, "platform", "template")
    assert (
        validate_selected_mappings(
            hass,
            entry,
            source,
            {
                "solar_production": EnvoyHistoryTarget(
                    flow_key="solar_production",
                    label="Solar",
                    unique_id=target_uid,
                    entity_id="sensor.site_solar_production",
                    current_value_kwh=1.1,
                )
            },
            {"solar_production": old_entity_id},
            require_source_unloaded=False,
        ).error
        == "incompatible_energy_total"
    )
    object.__setattr__(new_reg_entry, "platform", DOMAIN)
    hass.states.async_remove(new_entity_id)
    assert (
        validate_selected_mappings(
            hass,
            entry,
            source,
            {
                "solar_production": EnvoyHistoryTarget(
                    flow_key="solar_production",
                    label="Solar",
                    unique_id=target_uid,
                    entity_id="sensor.site_solar_production",
                    current_value_kwh=None,
                )
            },
            {"solar_production": old_entity_id},
            require_source_unloaded=False,
        ).error
        == "incompatible_energy_total"
    )


@pytest.mark.parametrize("with_old_state", [True, False])
def test_execute_takeover_archives_envoy_and_reassigns_target(
    hass, monkeypatch, with_old_state
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    source_entry = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-entry")
    entry.add_to_hass(hass)
    _patch_entry_lookup(monkeypatch, hass, source_entry)

    old_entity_id = _add_entity(
        hass,
        entry=source_entry,
        platform="enphase_envoy",
        unique_id="envoy_old",
        object_id="envoy_lifetime_production",
        state="4.0",
        attrs=_energy_attrs(),
    )
    new_entity_id = _add_entity(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("1", "solar_production"),
        object_id="site_solar_production",
        state="4.1",
        attrs=_energy_attrs(),
    )

    if not with_old_state:
        hass.states.async_remove(old_entity_id)

    error = execute_takeover(
        hass,
        [
            EnvoyHistoryMapping(
                flow_key="solar_production",
                label="Solar",
                old_entity_id=old_entity_id,
                archived_entity_id="sensor.envoy_lifetime_production_envoy_legacy",
                old_value_kwh=4.0,
                new_entity_id=new_entity_id,
                new_value_kwh=4.1,
                target_unique_id=migration_target_unique_id("1", "solar_production"),
            )
        ],
    )

    ent_reg = er.async_get(hass)
    assert error is None
    assert ent_reg.async_get(old_entity_id).platform == DOMAIN
    archived_entity = ent_reg.async_get("sensor.envoy_lifetime_production_envoy_legacy")
    assert archived_entity is not None
    assert archived_entity.platform == "enphase_envoy"
    assert archived_entity.disabled_by == er.RegistryEntryDisabler.USER
    assert ent_reg.async_get(new_entity_id) is None


def test_execute_takeover_reports_missing_old_or_target(hass, monkeypatch) -> None:
    error = execute_takeover(
        hass,
        [
            EnvoyHistoryMapping(
                flow_key="solar_production",
                label="Solar",
                old_entity_id="sensor.missing_old",
                archived_entity_id="sensor.archived",
                old_value_kwh=1.0,
                new_entity_id="sensor.site_solar_production",
                new_value_kwh=1.1,
                target_unique_id="uid",
            )
        ],
    )
    assert error is not None
    assert error.reason == "Source entity is no longer available."

    entry = MockConfigEntry(domain=DOMAIN, entry_id="entry", data={CONF_SITE_ID: "1"})
    source_entry = MockConfigEntry(domain="template", entry_id="source")
    entry.add_to_hass(hass)
    _patch_entry_lookup(monkeypatch, hass, source_entry)
    old_entity_id = _add_entity(
        hass,
        entry=source_entry,
        platform="template",
        unique_id="old",
        object_id="old_total",
        state="1",
        attrs=_energy_attrs(),
    )
    error = execute_takeover(
        hass,
        [
            EnvoyHistoryMapping(
                flow_key="solar_production",
                label="Solar",
                old_entity_id=old_entity_id,
                archived_entity_id="sensor.old_total_legacy",
                old_value_kwh=1.0,
                new_entity_id="sensor.site_solar_production",
                new_value_kwh=1.1,
                target_unique_id="missing_uid",
            )
        ],
    )
    assert error is not None
    assert error.reason == "Enphase Energy target entity is no longer available."


def test_execute_takeover_archives_external_source_and_reassigns_target(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    template_entry = MockConfigEntry(domain="template", entry_id="template-entry")
    entry.add_to_hass(hass)
    _patch_entry_lookup(monkeypatch, hass, template_entry)

    old_entity_id = _add_entity(
        hass,
        entry=template_entry,
        platform="template",
        unique_id="template_old",
        object_id="template_lifetime_production",
        state="4.0",
        attrs=_energy_attrs(),
    )
    new_entity_id = _add_entity(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("1", "solar_production"),
        object_id="site_solar_production",
        state="4.1",
        attrs=_energy_attrs(),
    )

    error = execute_takeover(
        hass,
        [
            EnvoyHistoryMapping(
                flow_key="solar_production",
                label="Solar",
                old_entity_id=old_entity_id,
                archived_entity_id="sensor.template_lifetime_production_legacy",
                old_value_kwh=4.0,
                new_entity_id=new_entity_id,
                new_value_kwh=4.1,
                target_unique_id=migration_target_unique_id("1", "solar_production"),
            )
        ],
    )

    ent_reg = er.async_get(hass)
    assert error is None
    assert ent_reg.async_get(old_entity_id).platform == DOMAIN
    archived_entity = ent_reg.async_get("sensor.template_lifetime_production_legacy")
    assert archived_entity is not None
    assert archived_entity.platform == "template"
    assert archived_entity.disabled_by == er.RegistryEntryDisabler.USER
    assert ent_reg.async_get(new_entity_id) is None


def test_execute_takeover_uses_unique_archive_id_when_legacy_name_exists(
    hass, monkeypatch
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    source_entry = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-entry")
    entry.add_to_hass(hass)
    _patch_entry_lookup(monkeypatch, hass, source_entry)

    old_entity_id = _add_entity(
        hass,
        entry=source_entry,
        platform="enphase_envoy",
        unique_id="envoy_old",
        object_id="envoy_lifetime_production",
        state="4.0",
        attrs=_energy_attrs(),
    )
    _add_entity(
        hass,
        entry=source_entry,
        platform="enphase_envoy",
        unique_id="envoy_old_legacy",
        object_id="envoy_lifetime_production_envoy_legacy",
        state="4.0",
        attrs=_energy_attrs(),
    )
    new_entity_id = _add_entity(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("1", "solar_production"),
        object_id="site_solar_production",
        state="4.1",
        attrs=_energy_attrs(),
    )
    source = EnvoyHistorySource(
        entry_id="envoy-entry",
        title="Envoy",
        candidates=[
            EnvoyHistoryCandidate(
                entity_id=old_entity_id,
                config_entry_id="envoy-entry",
                title="Old",
                current_value_kwh=4.0,
            )
        ],
    )
    targets = {
        "solar_production": EnvoyHistoryTarget(
            flow_key="solar_production",
            label="Solar",
            unique_id=migration_target_unique_id("1", "solar_production"),
            entity_id=new_entity_id,
            current_value_kwh=4.1,
        )
    }

    validation = validate_selected_mappings(
        hass,
        entry,
        source,
        targets,
        {"solar_production": old_entity_id},
        require_source_unloaded=False,
    )

    assert validation.error is None
    assert validation.mappings[0].archived_entity_id == (
        "sensor.envoy_lifetime_production_envoy_legacy_2"
    )


def test_execute_takeover_reports_partial_failure(hass, monkeypatch) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="enphase-entry", data={CONF_SITE_ID: "1"}
    )
    source_entry = MockConfigEntry(domain="enphase_envoy", entry_id="envoy-entry")
    entry.add_to_hass(hass)
    _patch_entry_lookup(monkeypatch, hass, source_entry)

    old_entity_id = _add_entity(
        hass,
        entry=source_entry,
        platform="enphase_envoy",
        unique_id="envoy_old",
        object_id="envoy_lifetime_production",
        state="4.0",
        attrs=_energy_attrs(),
    )
    new_entity_id = _add_entity(
        hass,
        entry=entry,
        platform=DOMAIN,
        unique_id=migration_target_unique_id("1", "solar_production"),
        object_id="site_solar_production",
        state="4.1",
        attrs=_energy_attrs(),
    )
    ent_reg = er.async_get(hass)
    monkeypatch.setattr(
        ent_reg,
        "async_update_entity",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    error = execute_takeover(
        hass,
        [
            EnvoyHistoryMapping(
                flow_key="solar_production",
                label="Solar",
                old_entity_id=old_entity_id,
                archived_entity_id="sensor.envoy_lifetime_production_envoy_legacy",
                old_value_kwh=4.0,
                new_entity_id=new_entity_id,
                new_value_kwh=4.1,
                target_unique_id=migration_target_unique_id("1", "solar_production"),
            )
        ],
    )

    assert error is not None
    assert error.completed == []
    assert error.reason == "boom"


def test_format_helpers_render_markdown_lists() -> None:
    targets = {
        "solar_production": EnvoyHistoryTarget(
            flow_key="solar_production",
            label="Solar",
            unique_id="uid",
            entity_id="sensor.site_solar_production",
            current_value_kwh=1.0,
        )
    }
    mapping = EnvoyHistoryMapping(
        flow_key="solar_production",
        label="Solar",
        old_entity_id="sensor.envoy_lifetime_production",
        archived_entity_id="sensor.envoy_lifetime_production_envoy_legacy",
        old_value_kwh=1.0,
        new_entity_id="sensor.site_solar_production",
        new_value_kwh=1.0,
        target_unique_id="uid",
    )

    assert format_mapping_preview([mapping]) == (
        "- Archive source entity: `sensor.envoy_lifetime_production` -> "
        "`sensor.envoy_lifetime_production_envoy_legacy`\n"
        "- Reassign Enphase Energy: `sensor.site_solar_production` -> "
        "`sensor.envoy_lifetime_production`"
    )
    assert (
        format_selection_preview(
            {"solar_production": "sensor.envoy_lifetime_production"}, targets
        )
        == "- Reassign Enphase Energy: `sensor.site_solar_production` -> "
        "`sensor.envoy_lifetime_production`"
    )
    assert format_completed_preview([mapping]) == (
        "- Archive source entity: `sensor.envoy_lifetime_production` -> "
        "`sensor.envoy_lifetime_production_envoy_legacy`\n"
        "- Reassign Enphase Energy: `sensor.site_solar_production` -> "
        "`sensor.envoy_lifetime_production`"
    )
    assert format_completed_preview([]) == "No mappings were completed."


def test_unknown_history_flow_has_no_candidate_score():
    assert (
        _score_candidate(
            "unknown", EnvoyHistoryCandidate("sensor.total", "external", "Total", 1)
        )
        == 0
    )


async def test_external_discovery_skips_non_energy_sensors(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SITE_ID: "1"})
    source = MockConfigEntry(domain="external")
    entry.add_to_hass(hass)
    source.add_to_hass(hass)
    _add_entity(
        hass,
        entry=source,
        platform="external",
        unique_id="temperature",
        object_id="temperature",
        state="20",
        attrs={"unit_of_measurement": "°C"},
    )
    assert await discover_external_migration_candidates(hass, entry) == []
