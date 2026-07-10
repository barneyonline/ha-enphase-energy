from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from urllib.error import URLError


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
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"domain": "enphase_ev", "quality_scale": quality_scale})
    )


def _write_reference(root: Path, path: str) -> None:
    reference = root / path
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text("ok")


def _write_quality_scale(root: Path, body: str) -> None:
    (root / "quality_scale.yaml").write_text(body)


class _FakeUrlopenResponse:
    def __init__(
        self,
        payload: object | None = None,
        *,
        status: int = 200,
        content_type: str = "image/png",
    ) -> None:
        self._payload = payload
        self.status = status
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def test_repository_quality_scale_matches_manifest_claim() -> None:
    root = Path(__file__).resolve().parents[2]

    exit_code, messages = validate_quality_scale.validate_quality_scale(root)

    assert exit_code == 0, "\n".join(messages)


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


def test_remote_brands_validation_requires_icon(monkeypatch, tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    monkeypatch.setattr(
        validate_quality_scale,
        "_fetch_brand_asset_names",
        lambda domain: {"logo.png"},
    )
    monkeypatch.setattr(
        validate_quality_scale,
        "_validate_brand_cdn_asset",
        lambda domain, asset: [],
    )

    messages = validate_quality_scale._validate_brands_support(tmp_path, remote=True)

    assert "missing icon.png" in "\n".join(messages)


def test_remote_brands_validation_checks_cdn_for_known_assets(
    monkeypatch, tmp_path: Path
) -> None:
    _write_manifest(tmp_path)
    checked_assets: list[str] = []
    monkeypatch.setattr(
        validate_quality_scale,
        "_fetch_brand_asset_names",
        lambda domain: {"icon.png", "logo.png"},
    )

    def _validate_asset(domain: str, asset: str) -> list[str]:
        checked_assets.append(asset)
        return []

    monkeypatch.setattr(
        validate_quality_scale,
        "_validate_brand_cdn_asset",
        _validate_asset,
    )

    messages = validate_quality_scale._validate_brands_support(tmp_path, remote=True)

    assert messages == []
    assert checked_assets == ["icon.png", "logo.png"]


def test_remote_brands_validation_reports_fetch_failures(
    monkeypatch, tmp_path: Path
) -> None:
    _write_manifest(tmp_path)

    def _raise_fetch_error(domain: str) -> set[str]:
        raise URLError("offline")

    monkeypatch.setattr(
        validate_quality_scale,
        "_fetch_brand_asset_names",
        _raise_fetch_error,
    )

    messages = validate_quality_scale._validate_brands_support(tmp_path, remote=True)

    assert "Could not validate brands repository assets" in "\n".join(messages)


def test_fetch_brand_asset_names_reads_github_listing(monkeypatch) -> None:
    def _urlopen(request, timeout):
        assert "custom_integrations/enphase_ev" in request.full_url
        assert timeout == 10
        return _FakeUrlopenResponse(
            [{"name": "icon.png"}, {"name": "logo.png"}, {"unexpected": True}]
        )

    monkeypatch.setattr(validate_quality_scale, "urlopen", _urlopen)

    assert validate_quality_scale._fetch_brand_asset_names("enphase_ev") == {
        "icon.png",
        "logo.png",
    }


def test_fetch_brand_asset_names_rejects_non_directory_listing(monkeypatch) -> None:
    monkeypatch.setattr(
        validate_quality_scale,
        "urlopen",
        lambda request, timeout: _FakeUrlopenResponse({"name": "icon.png"}),
    )

    try:
        validate_quality_scale._fetch_brand_asset_names("enphase_ev")
    except ValueError as err:
        assert "not a directory listing" in str(err)
    else:
        raise AssertionError("Expected ValueError")


def test_validate_brand_cdn_asset_accepts_png(monkeypatch) -> None:
    def _urlopen(request, timeout):
        assert request.get_method() == "HEAD"
        assert request.full_url.endswith("/enphase_ev/icon.png")
        assert timeout == 10
        return _FakeUrlopenResponse(status=200, content_type="image/png")

    monkeypatch.setattr(validate_quality_scale, "urlopen", _urlopen)

    assert (
        validate_quality_scale._validate_brand_cdn_asset("enphase_ev", "icon.png") == []
    )


def test_validate_brand_cdn_asset_reports_url_errors(monkeypatch) -> None:
    def _urlopen(request, timeout):
        raise URLError("offline")

    monkeypatch.setattr(validate_quality_scale, "urlopen", _urlopen)

    assert "Could not validate brands CDN asset" in "\n".join(
        validate_quality_scale._validate_brand_cdn_asset("enphase_ev", "icon.png")
    )


def test_validate_brand_cdn_asset_reports_status_and_content_type(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        validate_quality_scale,
        "urlopen",
        lambda request, timeout: _FakeUrlopenResponse(status=500),
    )
    assert "HTTP 500" in "\n".join(
        validate_quality_scale._validate_brand_cdn_asset("enphase_ev", "icon.png")
    )

    monkeypatch.setattr(
        validate_quality_scale,
        "urlopen",
        lambda request, timeout: _FakeUrlopenResponse(
            status=200, content_type="text/html"
        ),
    )
    assert "content-type" in "\n".join(
        validate_quality_scale._validate_brand_cdn_asset("enphase_ev", "icon.png")
    )


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


def test_gold_quality_claim_skips_platinum_strict_typing_contract(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[2]
    strict_validation_called = False

    def _strict_typing_errors(_root: Path) -> list[str]:
        nonlocal strict_validation_called
        strict_validation_called = True
        return ["strict error"]

    monkeypatch.setattr(
        validate_quality_scale,
        "_validate_brands_support",
        lambda root, *, remote=False: [],
    )
    monkeypatch.setattr(
        validate_quality_scale,
        "_validate_strict_typing_contract",
        _strict_typing_errors,
    )

    exit_code, messages = validate_quality_scale.validate_quality_scale(root)

    assert exit_code == 0, "\n".join(messages)
    assert strict_validation_called is False
