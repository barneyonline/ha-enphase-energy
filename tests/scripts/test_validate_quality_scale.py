from __future__ import annotations

import importlib.util
from datetime import date
import json
from pathlib import Path
import sys


def _load_module():
    root = Path(__file__).resolve().parents[2]
    module_path = root / "scripts" / "validate_quality_scale.py"
    spec = importlib.util.spec_from_file_location("validate_quality_scale", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validate_quality_scale = _load_module()


def _write_manifest(root: Path, quality_scale: str = "platinum") -> None:
    manifest_dir = root / "custom_components" / "enphase_ev"
    manifest_dir.mkdir(parents=True)
    brand_dir = manifest_dir / "brand"
    brand_dir.mkdir()
    (brand_dir / "icon.png").write_bytes(validate_quality_scale.PNG_SIGNATURE)
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"domain": "enphase_ev", "quality_scale": quality_scale})
    )


def _write_reference(root: Path, path: str) -> None:
    reference = root / path
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text("ok")


def _write_quality_scale(root: Path, body: str) -> None:
    (root / "quality_scale.yaml").write_text(body)


def _write_strict_mypy_gate(
    root: Path,
    *,
    command: str = validate_quality_scale.STRICT_TYPING_COMMAND,
    requirement: str = validate_quality_scale.PINNED_MYPY_REQUIREMENT,
) -> None:
    requirements = root / "devtools" / "docker" / "requirements-dev.txt"
    requirements.parent.mkdir(parents=True, exist_ok=True)
    requirements.write_text(f"{requirement}\n")
    workflow = root / ".github" / "workflows" / "tests.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(
        "jobs:\n"
        "  quality-scale:\n"
        "    steps:\n"
        "      - name: Validate strict typing for full integration package\n"
        f"        run: {command}\n"
    )


def test_repository_quality_scale_matches_manifest_claim() -> None:
    root = Path(__file__).resolve().parents[2]

    exit_code, messages = validate_quality_scale.validate_quality_scale(root)

    assert exit_code == 0, "\n".join(messages)


def test_official_bronze_catalog_tracks_automation_documentation_rules() -> None:
    bronze_rules = set(validate_quality_scale.OFFICIAL_REQUIRED_RULES["bronze"])

    assert {"docs-triggers", "docs-conditions"} <= bronze_rules
    assert "docs-conditions" in validate_quality_scale.NA_ALLOWED_RULES


def test_rule_catalog_metadata_records_canonical_review() -> None:
    valid = {
        "official_rule_catalog": {
            "source": validate_quality_scale.OFFICIAL_RULE_CATALOG_SOURCE,
            "reviewed_on": date(2026, 7, 10),
        }
    }

    assert validate_quality_scale._validate_rule_catalog_metadata(valid) == []
    assert validate_quality_scale._validate_rule_catalog_metadata({}) == [
        "official_rule_catalog metadata is missing"
    ]

    wrong_source = {
        "official_rule_catalog": {
            "source": "https://example.test/rules",
            "reviewed_on": "not-a-date",
        }
    }
    assert validate_quality_scale._validate_rule_catalog_metadata(wrong_source) == [
        "official_rule_catalog.source must reference the canonical Home Assistant rule list",
        "official_rule_catalog.reviewed_on must be an ISO date",
    ]


def test_trigger_documentation_must_name_every_implemented_trigger(
    tmp_path: Path,
) -> None:
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (integration_dir / "device_trigger.py").write_text(
        "TRIGGER_MAP = {'charging_started': {}, 'charging_stopped': {}}\n"
    )
    _write_reference(tmp_path, "docs/device_automations.md")
    (tmp_path / "docs" / "device_automations.md").write_text(
        "The `charging_started` trigger starts an automation.\n"
    )

    messages = validate_quality_scale._validate_trigger_documentation(
        tmp_path, ["docs/device_automations.md"]
    )

    assert messages == [
        "docs-triggers references do not document trigger type charging_stopped"
    ]


def test_trigger_documentation_handles_missing_and_invalid_declarations(
    tmp_path: Path,
) -> None:
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    trigger_path = integration_dir / "device_trigger.py"

    assert validate_quality_scale._device_trigger_types(tmp_path) == set()
    assert validate_quality_scale._validate_trigger_documentation(tmp_path, []) == [
        "docs-triggers could not find any declared device trigger types"
    ]

    integration_dir.mkdir(parents=True)
    trigger_path.write_text("def broken(:\n")
    assert validate_quality_scale._device_trigger_types(tmp_path) == set()

    trigger_path.write_text("OTHER = {}\n")
    assert validate_quality_scale._device_trigger_types(tmp_path) == set()

    trigger_path.write_text("OTHER = {}\nTRIGGER_MAP = make_map()\n")
    assert validate_quality_scale._device_trigger_types(tmp_path) == set()

    trigger_path.write_text("TRIGGER_MAP: dict = {'valid': {}, 1: {}}\n")
    assert validate_quality_scale._device_trigger_types(tmp_path) == {"valid"}


def test_trigger_documentation_requires_yaml_fields(tmp_path: Path) -> None:
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (integration_dir / "device_trigger.py").write_text(
        "TRIGGER_MAP = {'charging_started': {}}\n"
    )
    _write_reference(tmp_path, "docs/device_automations.md")
    (tmp_path / "docs" / "device_automations.md").write_text(
        "The charging_started trigger uses type: charging_started.\n"
    )

    messages = validate_quality_scale._validate_trigger_documentation(
        tmp_path, ["docs/device_automations.md"]
    )

    assert messages == [
        "docs-triggers references do not document YAML field platform: device",
        "docs-triggers references do not document YAML field domain: enphase_ev",
        "docs-triggers references do not document YAML field device_id:",
        "docs-triggers references do not document YAML field entity_id:",
    ]


def test_documentation_rules_require_docs_references(
    monkeypatch, tmp_path: Path
) -> None:
    _write_manifest(tmp_path, "bronze")
    _write_reference(tmp_path, "custom_components/enphase_ev/device_trigger.py")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [docs-triggers]
rules:
  docs-triggers:
    status: done
    references:
      code: [custom_components/enphase_ev/device_trigger.py]
""",
    )
    monkeypatch.setitem(
        validate_quality_scale.OFFICIAL_REQUIRED_RULES,
        "bronze",
        ("docs-triggers",),
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "Documentation rules missing docs references" in "\n".join(messages)


def test_validator_reports_incomplete_trigger_documentation(
    monkeypatch, tmp_path: Path
) -> None:
    _write_manifest(tmp_path, "bronze")
    _write_reference(tmp_path, "docs/device_automations.md")
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    (integration_dir / "device_trigger.py").write_text(
        "TRIGGER_MAP = {'charging_started': {}}\n"
    )
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [docs-triggers]
rules:
  docs-triggers:
    status: done
    references:
      docs: [docs/device_automations.md]
""",
    )
    monkeypatch.setitem(
        validate_quality_scale.OFFICIAL_REQUIRED_RULES,
        "bronze",
        ("docs-triggers",),
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    joined = "\n".join(messages)
    assert exit_code == 1
    assert "Incomplete rule documentation" in joined
    assert "charging_started" in joined


def test_gold_claim_requires_bronze_silver_and_gold_rules(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "gold")
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [config-flow]
  silver:
    required: [config-entry-unloading]
  gold:
    required: [devices]
rules:
  config-flow:
    status: done
    references:
      docs: [README.md]
  config-entry-unloading:
    status: done
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "devices" in "\n".join(messages)


def test_na_rules_need_explanatory_comments(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "bronze")
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [discovery-update-info]
rules:
  discovery-update-info:
    status: n/a
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "n/a rules missing explanatory comments" in "\n".join(messages)


def test_na_status_is_restricted_to_allowlisted_rules(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "bronze")
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [config-flow]
rules:
  config-flow:
    status: n/a
    comment: Not actually acceptable for this rule.
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "Rules marked n/a without an allowlist exception" in "\n".join(messages)


def test_brands_rule_cannot_be_marked_na(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "bronze")
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [brands]
rules:
  brands:
    status: n/a
    comment: Custom integration.
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "Rules marked n/a without an allowlist exception" in "\n".join(messages)


def test_local_brands_validation_requires_icon(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    (tmp_path / "custom_components/enphase_ev/brand/icon.png").unlink()

    for remote in (False, True):
        messages = validate_quality_scale._validate_brands_support(
            tmp_path, remote=remote
        )
        assert "missing icon.png" in "\n".join(messages)


def test_local_brands_validation_accepts_all_supported_assets(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    brand_dir = tmp_path / "custom_components/enphase_ev/brand"
    for asset in validate_quality_scale.OPTIONAL_BRAND_ASSETS:
        (brand_dir / asset).write_bytes(validate_quality_scale.PNG_SIGNATURE)

    for remote in (False, True):
        assert (
            validate_quality_scale._validate_brands_support(tmp_path, remote=remote)
            == []
        )


def test_local_brands_validation_rejects_non_png_assets(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    brand_dir = tmp_path / "custom_components/enphase_ev/brand"
    (brand_dir / "logo.png").write_text("<html>not an image</html>")
    (brand_dir / "icon.png").write_bytes(b"")

    messages = validate_quality_scale._validate_brands_support(tmp_path)

    assert messages == [
        "ERROR: Local brand asset icon.png is not a PNG image",
        "ERROR: Local brand asset logo.png is not a PNG image",
    ]


def test_local_brands_validation_reports_unreadable_assets(
    monkeypatch, tmp_path: Path
) -> None:
    _write_manifest(tmp_path)
    original_open = Path.open

    def _open(path, *args, **kwargs):
        if path.name == "icon.png":
            raise PermissionError("unreadable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)

    messages = validate_quality_scale._validate_brands_support(tmp_path)

    assert messages == ["ERROR: Could not read local brand asset icon.png: unreadable"]


def test_validate_brands_support_reports_manifest_domain_errors(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path)
    manifest = tmp_path / "custom_components" / "enphase_ev" / "manifest.json"
    manifest.write_text(json.dumps({"quality_scale": "platinum"}))

    messages = validate_quality_scale._validate_brands_support(tmp_path)

    assert "does not define a domain" in "\n".join(messages)


def test_rule_references_must_exist(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "bronze")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [config-flow]
rules:
  config-flow:
    status: done
    references:
      code: [custom_components/enphase_ev/config_flow.py]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "Rule references that do not exist" in "\n".join(messages)


def test_manifest_quality_scale_must_be_supported(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "diamond")
    _write_quality_scale(
        tmp_path,
        """
levels:
  silver:
    required: [config-flow]
rules:
  config-flow:
    status: done
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 2
    assert "Unsupported manifest quality_scale value" in "\n".join(messages)


def test_manifest_must_exist(tmp_path: Path) -> None:
    _write_quality_scale(tmp_path, "levels: {}\nrules: {}\n")

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 2
    assert "manifest.json" in "\n".join(messages)


def test_quality_scale_file_must_exist(tmp_path: Path) -> None:
    _write_manifest(tmp_path)

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 2
    assert "quality_scale.yaml" in "\n".join(messages)


def test_required_level_must_be_defined(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "bronze")
    _write_quality_scale(tmp_path, "levels: {}\nrules: {}\n")

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 2
    assert "No bronze.required" in "\n".join(messages)


def test_rules_must_have_references_and_done_status(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "bronze")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [config-flow]
rules:
  config-flow:
    status: pending
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    joined = "\n".join(messages)
    assert exit_code == 1
    assert "Rules not marked done" in joined
    assert "Rules missing code/test/doc references" in joined


def test_required_levels_must_include_official_rule_ids(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "bronze")
    _write_reference(tmp_path, "README.md")
    _write_quality_scale(
        tmp_path,
        """
levels:
  bronze:
    required: [config-flow]
rules:
  config-flow:
    status: done
    references:
      docs: [README.md]
""",
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(tmp_path)

    assert exit_code == 1
    assert "Required official rules are missing from levels" in "\n".join(messages)
    assert "bronze: action-setup" in "\n".join(messages)


def test_strict_typing_contract_rejects_any_config_entry_alias(
    tmp_path: Path,
) -> None:
    (tmp_path / ".strict-typing").write_text("custom_components/enphase_ev\n")
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (integration_dir / "py.typed").write_text("")
    (integration_dir / "runtime_data.py").write_text(
        "EnphaseConfigEntry: TypeAlias = ConfigEntry[EnphaseRuntimeData]\n"
        "EnphaseConfigEntry = Any\n"
    )

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)

    assert "must not fall back to Any" in "\n".join(messages)


def test_strict_typing_contract_requires_marker_files(tmp_path: Path) -> None:
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (integration_dir / "runtime_data.py").write_text("")

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)
    joined = "\n".join(messages)

    assert ".strict-typing is missing" in joined
    assert "py.typed is missing" in joined
    assert "must define EnphaseConfigEntry" in joined


def test_strict_typing_contract_requires_strict_typing_entry(
    tmp_path: Path,
) -> None:
    (tmp_path / ".strict-typing").write_text("custom_components/other\n")
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (integration_dir / "py.typed").write_text("")
    (integration_dir / "runtime_data.py").write_text(
        "EnphaseConfigEntry: TypeAlias = ConfigEntry[EnphaseRuntimeData]\n"
    )

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)

    assert "must include custom_components/enphase_ev" in "\n".join(messages)


def test_strict_typing_contract_accepts_python314_type_alias(
    tmp_path: Path,
) -> None:
    _write_strict_mypy_gate(tmp_path)
    (tmp_path / ".strict-typing").write_text("custom_components/enphase_ev\n")
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (integration_dir / "py.typed").write_text("")
    (integration_dir / "runtime_data.py").write_text(
        "type EnphaseConfigEntry = ConfigEntry[EnphaseRuntimeData]\n"
    )

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)

    assert messages == []


def test_strict_typing_contract_reports_missing_runtime_data(
    tmp_path: Path,
) -> None:
    (tmp_path / ".strict-typing").write_text("custom_components/enphase_ev\n")
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (integration_dir / "py.typed").write_text("")

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)

    assert "runtime_data.py is missing" in "\n".join(messages)


def test_strict_typing_contract_rejects_bare_config_entry_use(
    tmp_path: Path,
) -> None:
    (tmp_path / ".strict-typing").write_text("custom_components/enphase_ev\n")
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (integration_dir / "py.typed").write_text("")
    (integration_dir / "runtime_data.py").write_text(
        "EnphaseConfigEntry: TypeAlias = ConfigEntry[EnphaseRuntimeData]\n"
    )
    (integration_dir / "config_flow.py").write_text(
        "from homeassistant.config_entries import ConfigEntry\n"
    )

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)

    assert "bare ConfigEntry" in "\n".join(messages)


def test_annotation_detects_bare_config_entry_name() -> None:
    annotation = (
        validate_quality_scale.ast.parse("entry: ConfigEntry").body[0].annotation
    )

    assert validate_quality_scale._annotation_uses_bare_config_entry(annotation) is True


def test_strict_typing_contract_rejects_config_entries_config_entry_annotation(
    tmp_path: Path,
) -> None:
    (tmp_path / ".strict-typing").write_text("custom_components/enphase_ev\n")
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (integration_dir / "py.typed").write_text("")
    (integration_dir / "runtime_data.py").write_text(
        "EnphaseConfigEntry: TypeAlias = ConfigEntry[EnphaseRuntimeData]\n"
    )
    (integration_dir / "config_flow.py").write_text(
        "from homeassistant import config_entries\n"
        "def bad(entry: config_entries.ConfigEntry) -> None:\n"
        "    return None\n"
    )

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)

    assert "config_flow.py:2" in "\n".join(messages)


def test_strict_typing_contract_allows_marked_external_config_entry_annotation(
    tmp_path: Path,
) -> None:
    _write_strict_mypy_gate(tmp_path)
    (tmp_path / ".strict-typing").write_text("custom_components/enphase_ev\n")
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (integration_dir / "py.typed").write_text("")
    (integration_dir / "runtime_data.py").write_text(
        "EnphaseConfigEntry: TypeAlias = ConfigEntry[EnphaseRuntimeData]\n"
    )
    (integration_dir / "config_flow.py").write_text(
        "from homeassistant import config_entries\n"
        "def ok(entry: config_entries.ConfigEntry) -> None:  "
        "# quality-scale: external-config-entry\n"
        "    return None\n"
    )

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)

    assert messages == []


def test_strict_typing_contract_requires_pinned_mypy(tmp_path: Path) -> None:
    _write_strict_mypy_gate(tmp_path, requirement="mypy")
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (tmp_path / ".strict-typing").write_text("custom_components/enphase_ev\n")
    (integration_dir / "py.typed").write_text("")
    (integration_dir / "runtime_data.py").write_text(
        "type EnphaseConfigEntry = ConfigEntry[EnphaseRuntimeData]\n"
    )

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)

    assert "must pin mypy==2.2.0" in "\n".join(messages)


def test_strict_typing_contract_rejects_partial_mypy_gate(tmp_path: Path) -> None:
    _write_strict_mypy_gate(
        tmp_path,
        command=(
            "mypy --strict --ignore-missing-imports --follow-imports=skip "
            "custom_components/enphase_ev/runtime_data.py "
            "custom_components/enphase_ev/config_flow.py"
        ),
    )
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (tmp_path / ".strict-typing").write_text("custom_components/enphase_ev\n")
    (integration_dir / "py.typed").write_text("")
    (integration_dir / "runtime_data.py").write_text(
        "type EnphaseConfigEntry = ConfigEntry[EnphaseRuntimeData]\n"
    )

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)

    joined = "\n".join(messages)
    assert "exact full-package strict typing command" in joined
    assert validate_quality_scale.STRICT_TYPING_COMMAND in joined


def test_strict_typing_contract_reports_invalid_workflow_yaml(tmp_path: Path) -> None:
    _write_strict_mypy_gate(tmp_path)
    (tmp_path / ".github" / "workflows" / "tests.yml").write_text("jobs: [\n")

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)

    assert "tests.yml is invalid YAML" in "\n".join(messages)


def test_strict_typing_contract_requires_quality_scale_steps(tmp_path: Path) -> None:
    _write_strict_mypy_gate(tmp_path)
    (tmp_path / ".github" / "workflows" / "tests.yml").write_text(
        "jobs:\n  quality-scale:\n    steps: invalid\n"
    )

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)

    assert "exact full-package strict typing command" in "\n".join(messages)


def test_strict_typing_contract_reports_syntax_errors(tmp_path: Path) -> None:
    (tmp_path / ".strict-typing").write_text("custom_components/enphase_ev\n")
    integration_dir = tmp_path / "custom_components" / "enphase_ev"
    integration_dir.mkdir(parents=True)
    (integration_dir / "py.typed").write_text("")
    (integration_dir / "runtime_data.py").write_text(
        "EnphaseConfigEntry: TypeAlias = ConfigEntry[EnphaseRuntimeData]\n"
    )
    (integration_dir / "broken.py").write_text("def broken(:\n")

    messages = validate_quality_scale._validate_strict_typing_contract(tmp_path)

    assert "is not valid Python" in "\n".join(messages)


def test_small_helpers_cover_edge_shapes(tmp_path: Path) -> None:
    assert validate_quality_scale._status_for_rule(" DONE ") == "done"
    assert validate_quality_scale._status_for_rule(None) == ""
    assert validate_quality_scale._references_for_rule(None) == {}
    assert validate_quality_scale._references_for_rule({"references": []}) == {}
    assert validate_quality_scale._references_for_rule(
        {"references": {"docs": "README.md"}}
    ) == {"docs": ["README.md"]}
    assert validate_quality_scale._reference_path_exists(tmp_path, "") is False
    assert validate_quality_scale._annotation_uses_bare_config_entry(None) is False


def test_main_reports_success_and_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["validate_quality_scale.py"])
    monkeypatch.setattr(
        validate_quality_scale,
        "validate_quality_scale",
        lambda root, *, validate_remote_brands=False: (0, ["ok"]),
    )

    assert validate_quality_scale.main() == 0
    assert "ok" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["validate_quality_scale.py"])
    monkeypatch.setattr(
        validate_quality_scale,
        "validate_quality_scale",
        lambda root, *, validate_remote_brands=False: (1, ["bad"]),
    )

    assert validate_quality_scale.main() == 1
    assert "bad" in capsys.readouterr().err


def test_main_passes_remote_brands_flag(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        sys, "argv", ["validate_quality_scale.py", "--validate-remote-brands"]
    )

    def _validate(root, *, validate_remote_brands=False):
        calls.append(validate_remote_brands)
        return 0, ["ok"]

    monkeypatch.setattr(validate_quality_scale, "validate_quality_scale", _validate)

    assert validate_quality_scale.main() == 0
    assert calls == [True]


def test_validate_quality_scale_returns_brand_errors(
    monkeypatch, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(
        validate_quality_scale,
        "_validate_brands_support",
        lambda root, *, remote=False: ["brand error"],
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(
        root, validate_remote_brands=True
    )

    assert exit_code == 1
    assert messages == ["brand error"]


def test_validate_quality_scale_returns_strict_typing_errors(
    monkeypatch, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(
        validate_quality_scale,
        "_validate_brands_support",
        lambda root, *, remote=False: [],
    )
    monkeypatch.setattr(
        validate_quality_scale,
        "_validate_strict_typing_contract",
        lambda root: ["strict error"],
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(root)

    assert exit_code == 1
    assert messages == ["strict error"]
