#!/usr/bin/env python3
from __future__ import annotations

import ast
import argparse
from datetime import date
import json
from http import HTTPStatus
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - exercised only when dev deps are missing.
    print(
        "ERROR: PyYAML is required. Install with `pip install pyyaml`.", file=sys.stderr
    )
    raise

QUALITY_LEVEL_ORDER = ("bronze", "silver", "gold", "platinum")
OFFICIAL_RULE_CATALOG_SOURCE = (
    "https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/"
)
REQUIRED_DEVICE_TRIGGER_DOC_SNIPPETS = (
    "platform: device",
    "domain: enphase_ev",
    "device_id:",
    "entity_id:",
    "type:",
)
OFFICIAL_REQUIRED_RULES: dict[str, tuple[str, ...]] = {
    "bronze": (
        "action-setup",
        "appropriate-polling",
        "brands",
        "common-modules",
        "config-flow-test-coverage",
        "config-flow",
        "dependency-transparency",
        "docs-actions",
        "docs-triggers",
        "docs-conditions",
        "docs-high-level-description",
        "docs-installation-instructions",
        "docs-removal-instructions",
        "entity-event-setup",
        "entity-unique-id",
        "has-entity-name",
        "runtime-data",
        "test-before-configure",
        "test-before-setup",
        "unique-config-entry",
    ),
    "silver": (
        "action-exceptions",
        "config-entry-unloading",
        "docs-configuration-parameters",
        "docs-installation-parameters",
        "entity-unavailable",
        "integration-owner",
        "log-when-unavailable",
        "parallel-updates",
        "reauthentication-flow",
        "test-coverage",
    ),
    "gold": (
        "devices",
        "diagnostics",
        "discovery-update-info",
        "discovery",
        "docs-data-update",
        "docs-examples",
        "docs-known-limitations",
        "docs-supported-devices",
        "docs-supported-functions",
        "docs-troubleshooting",
        "docs-use-cases",
        "dynamic-devices",
        "entity-category",
        "entity-device-class",
        "entity-disabled-by-default",
        "entity-translations",
        "exception-translations",
        "icon-translations",
        "reconfiguration-flow",
        "repair-issues",
        "stale-devices",
    ),
    "platinum": (
        "async-dependency",
        "inject-websession",
        "strict-typing",
    ),
}
NA_ALLOWED_RULES = {"discovery", "discovery-update-info", "docs-conditions"}
STRICT_CONFIG_ENTRY_ALIASES = (
    "EnphaseConfigEntry: TypeAlias = ConfigEntry[EnphaseRuntimeData]",
    "type EnphaseConfigEntry = ConfigEntry[EnphaseRuntimeData]",
)
EXTERNAL_CONFIG_ENTRY_MARKER = "quality-scale: external-config-entry"
STRICT_TYPING_COMMAND = (
    "mypy --strict --ignore-missing-imports --follow-imports=skip "
    "custom_components/enphase_ev"
)
PINNED_MYPY_REQUIREMENT = "mypy==2.2.0"
BRANDS_GITHUB_API_URL = (
    "https://api.github.com/repos/home-assistant/brands/contents/"
    "custom_integrations/{domain}"
)
BRANDS_CDN_URL = "https://brands.home-assistant.io/{domain}/{asset}"
REQUIRED_BRAND_ASSETS = ("icon.png",)
OPTIONAL_BRAND_ASSETS = ("logo.png",)


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def _validate_rule_catalog_metadata(data: dict) -> list[str]:
    """Record a review against the canonical Home Assistant rule list."""

    metadata = data.get("official_rule_catalog")
    if not isinstance(metadata, dict):
        return ["official_rule_catalog metadata is missing"]

    messages: list[str] = []
    source = str(metadata.get("source") or "").strip()
    if source != OFFICIAL_RULE_CATALOG_SOURCE:
        messages.append(
            "official_rule_catalog.source must reference the canonical Home "
            "Assistant rule list"
        )

    raw_reviewed = metadata.get("reviewed_on")
    if not isinstance(raw_reviewed, date):
        try:
            date.fromisoformat(str(raw_reviewed or ""))
        except ValueError:
            messages.append("official_rule_catalog.reviewed_on must be an ISO date")
            return messages

    return messages


def _status_for_rule(entry: object) -> str:
    if isinstance(entry, dict):
        return str(entry.get("status") or "").strip().lower()
    if isinstance(entry, str):
        return entry.strip().lower()
    return ""


def _references_for_rule(entry: object) -> dict[str, list[str]]:
    if not isinstance(entry, dict):
        return {}
    references = entry.get("references")
    if not isinstance(references, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for key in ("code", "tests", "docs"):
        values = references.get(key)
        if isinstance(values, str):
            normalized[key] = [values]
        elif isinstance(values, list):
            normalized[key] = [str(value) for value in values if str(value).strip()]
    return normalized


def _claimed_level(root: Path) -> str:
    manifest_path = root / "custom_components" / "enphase_ev" / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"{manifest_path} not found")
    manifest = json.loads(manifest_path.read_text())
    level = str(manifest.get("quality_scale") or "silver").strip().lower()
    if level not in QUALITY_LEVEL_ORDER:
        raise ValueError(f"Unsupported manifest quality_scale value: {level!r}")
    return level


def _required_levels_for_claim(level: str) -> tuple[str, ...]:
    claimed_index = QUALITY_LEVEL_ORDER.index(level)
    return QUALITY_LEVEL_ORDER[: claimed_index + 1]


def _reference_path_exists(root: Path, reference: str) -> bool:
    path_text = reference.split("#", 1)[0].strip()
    if not path_text:
        return False
    return (root / path_text).exists()


def _device_trigger_types(root: Path) -> set[str]:
    """Return trigger type keys declared by the integration's TRIGGER_MAP."""

    path = root / "custom_components" / "enphase_ev" / "device_trigger.py"
    if not path.exists():
        return set()
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name) and target.id == "TRIGGER_MAP"
            for target in targets
        ):
            continue
        value = node.value
        if not isinstance(value, ast.Dict):
            return set()
        return {
            key.value
            for key in value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    return set()


def _validate_trigger_documentation(root: Path, doc_references: list[str]) -> list[str]:
    """Ensure every implemented device trigger is named in referenced docs."""

    trigger_types = _device_trigger_types(root)
    if not trigger_types:
        return ["docs-triggers could not find any declared device trigger types"]
    docs_text: list[str] = []
    for reference in doc_references:
        path = root / reference.split("#", 1)[0].strip()
        if path.is_file():
            docs_text.append(path.read_text())
    combined = "\n".join(docs_text).casefold()
    missing = sorted(trigger for trigger in trigger_types if trigger not in combined)
    if missing:
        return [
            "docs-triggers references do not document trigger type " + trigger
            for trigger in missing
        ]
    missing_yaml = [
        snippet
        for snippet in REQUIRED_DEVICE_TRIGGER_DOC_SNIPPETS
        if snippet not in combined
    ]
    return [
        "docs-triggers references do not document YAML field " + snippet
        for snippet in missing_yaml
    ]


def _manifest_domain(root: Path) -> str:
    manifest_path = root / "custom_components" / "enphase_ev" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    domain = str(manifest.get("domain") or "").strip()
    if not domain:
        raise ValueError(f"{manifest_path} does not define a domain")
    return domain


def _validate_brands_support(root: Path, *, remote: bool = False) -> list[str]:
    """Return brand support validation errors."""

    try:
        domain = _manifest_domain(root)
    except (json.JSONDecodeError, ValueError) as err:
        return [f"ERROR: {err}"]

    if not remote:
        return []

    messages: list[str] = []
    try:
        names = _fetch_brand_asset_names(domain)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as err:
        return [f"ERROR: Could not validate brands repository assets: {err}"]

    for asset in REQUIRED_BRAND_ASSETS:
        if asset not in names:
            messages.append(
                f"ERROR: home-assistant/brands is missing {asset} for {domain}"
            )

    for asset in (*REQUIRED_BRAND_ASSETS, *OPTIONAL_BRAND_ASSETS):
        if asset in names:
            messages.extend(_validate_brand_cdn_asset(domain, asset))

    return messages


def _fetch_brand_asset_names(domain: str) -> set[str]:
    request = Request(
        BRANDS_GITHUB_API_URL.format(domain=domain),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ha-enphase-ev-quality-scale-validator",
        },
    )
    with urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode())
    if not isinstance(payload, list):
        raise ValueError("GitHub brands response was not a directory listing")
    names: set[str] = set()
    for item in payload:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.add(item["name"])
    return names


def _validate_brand_cdn_asset(domain: str, asset: str) -> list[str]:
    request = Request(
        BRANDS_CDN_URL.format(domain=domain, asset=asset),
        method="HEAD",
        headers={"User-Agent": "ha-enphase-ev-quality-scale-validator"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            status = response.status
            content_type = response.headers.get("content-type", "")
    except (HTTPError, URLError, TimeoutError) as err:
        return [f"ERROR: Could not validate brands CDN asset {asset}: {err}"]

    if status != HTTPStatus.OK:
        return [f"ERROR: brands CDN asset {asset} returned HTTP {status}"]
    if not content_type.lower().startswith("image/png"):
        return [
            f"ERROR: brands CDN asset {asset} returned content-type {content_type!r}"
        ]
    return []


def _validate_strict_typing_contract(root: Path) -> list[str]:
    """Return strict typing contract errors for the integration package."""

    integration_root = root / "custom_components" / "enphase_ev"
    runtime_data_path = integration_root / "runtime_data.py"
    messages: list[str] = []

    requirements_path = root / "devtools" / "docker" / "requirements-dev.txt"
    if not requirements_path.exists():
        messages.append("ERROR: devtools/docker/requirements-dev.txt is missing")
    else:
        requirements = {
            line.strip()
            for line in requirements_path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        if PINNED_MYPY_REQUIREMENT not in requirements:
            messages.append(
                f"ERROR: dev requirements must pin {PINNED_MYPY_REQUIREMENT}"
            )

    workflow_path = root / ".github" / "workflows" / "tests.yml"
    if not workflow_path.exists():
        messages.append("ERROR: .github/workflows/tests.yml is missing")
    else:
        try:
            workflow = _load_yaml(workflow_path)
        except yaml.YAMLError as err:
            messages.append(
                f"ERROR: .github/workflows/tests.yml is invalid YAML: {err}"
            )
        else:
            jobs = workflow.get("jobs")
            quality_scale_job = (
                jobs.get("quality-scale") if isinstance(jobs, dict) else None
            )
            steps = (
                quality_scale_job.get("steps")
                if isinstance(quality_scale_job, dict)
                else None
            )
            if not isinstance(steps, list):
                steps = []
            strict_commands = {
                " ".join(run.split())
                for step in steps
                if isinstance(step, dict)
                if isinstance((run := step.get("run")), str)
                if run.lstrip().startswith("mypy ")
            }
            if STRICT_TYPING_COMMAND not in strict_commands:
                messages.append(
                    "ERROR: tests workflow must run the exact full-package strict "
                    f"typing command: {STRICT_TYPING_COMMAND}"
                )

    strict_typing_path = root / ".strict-typing"
    if not strict_typing_path.exists():
        messages.append("ERROR: .strict-typing is missing")
    elif (
        "custom_components/enphase_ev"
        not in strict_typing_path.read_text().splitlines()
    ):
        messages.append(
            "ERROR: .strict-typing must include custom_components/enphase_ev"
        )

    if not (integration_root / "py.typed").exists():
        messages.append("ERROR: custom_components/enphase_ev/py.typed is missing")

    if not runtime_data_path.exists():
        messages.append(
            "ERROR: custom_components/enphase_ev/runtime_data.py is missing"
        )
        return messages

    runtime_data = runtime_data_path.read_text()
    if not any(alias in runtime_data for alias in STRICT_CONFIG_ENTRY_ALIASES):
        messages.append(
            "ERROR: runtime_data.py must define EnphaseConfigEntry as "
            "ConfigEntry[EnphaseRuntimeData]"
        )
    if "EnphaseConfigEntry = Any" in runtime_data:
        messages.append(
            "ERROR: runtime_data.py must not fall back to Any for EnphaseConfigEntry"
        )

    bad_config_entry_uses: list[str] = []
    for path in sorted(integration_root.glob("*.py")):
        if path.name == "runtime_data.py":
            continue
        source = path.read_text()
        source_lines = source.splitlines()
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as err:
            messages.append(
                f"ERROR: {path.relative_to(root)} is not valid Python: {err}"
            )
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "homeassistant.config_entries" and any(
                    alias.name == "ConfigEntry" for alias in node.names
                ):
                    bad_config_entry_uses.append(
                        f"{path.relative_to(root)}:{node.lineno}"
                    )
                continue

            annotations: list[ast.expr | None] = []
            if isinstance(node, ast.arg):
                annotations.append(node.annotation)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotations.append(node.returns)
            elif isinstance(node, ast.AnnAssign):
                annotations.append(node.annotation)

            for annotation in annotations:
                if not _annotation_uses_bare_config_entry(annotation):
                    continue
                start = max((node.lineno or 1) - 1, 0)
                end = getattr(node, "end_lineno", None) or node.lineno or 1
                annotation_source_lines = source_lines[start:end]
                if any(
                    EXTERNAL_CONFIG_ENTRY_MARKER in line
                    for line in annotation_source_lines
                ):
                    continue
                bad_config_entry_uses.append(f"{path.relative_to(root)}:{node.lineno}")

    if bad_config_entry_uses:
        messages.append(
            "ERROR: use EnphaseConfigEntry from runtime_data.py instead of importing "
            "or annotating with bare ConfigEntry in integration modules: "
            + ", ".join(bad_config_entry_uses)
        )

    return messages


def _annotation_uses_bare_config_entry(annotation: ast.expr | None) -> bool:
    """Return True if an annotation references an untyped ConfigEntry."""

    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id == "ConfigEntry":
            return True
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "ConfigEntry"
            and isinstance(node.value, ast.Name)
            and node.value.id == "config_entries"
        ):
            return True
    return False


def validate_quality_scale(
    root: Path, *, validate_remote_brands: bool = False
) -> tuple[int, list[str]]:
    qs_path = root / "quality_scale.yaml"
    if not qs_path.exists():
        return 2, [f"ERROR: {qs_path} not found"]
    try:
        claimed_level = _claimed_level(root)
    except (json.JSONDecodeError, ValueError) as err:
        return 2, [f"ERROR: {err}"]

    data = _load_yaml(qs_path)
    levels = data.get("levels") or {}
    rules = data.get("rules") or {}

    messages: list[str] = []
    rule_catalog_errors = _validate_rule_catalog_metadata(data)
    missing: list[str] = []
    missing_official_rules: list[tuple[str, str]] = []
    not_accepted: list[tuple[str, str]] = []
    not_allowed_na: list[str] = []
    missing_references: list[str] = []
    missing_comments: list[str] = []
    bad_references: list[tuple[str, str]] = []
    missing_doc_references: list[str] = []
    incomplete_rule_docs: list[str] = []

    for level in _required_levels_for_claim(claimed_level):
        required = (levels.get(level) or {}).get("required") or []
        if not required:
            messages.append(
                f"ERROR: No {level}.required rules defined in quality_scale.yaml"
            )
            return 2, messages
        required_set = {str(rule) for rule in required}
        for official_rule in OFFICIAL_REQUIRED_RULES[level]:
            if official_rule not in required_set:
                missing_official_rules.append((level, official_rule))
        for rule in required:
            entry = rules.get(rule)
            if entry is None:
                missing.append(rule)
                continue
            status = _status_for_rule(entry)
            if status == "done":
                pass
            elif status == "n/a":
                if rule not in NA_ALLOWED_RULES:
                    not_allowed_na.append(rule)
                elif not (
                    isinstance(entry, dict) and str(entry.get("comment") or "").strip()
                ):
                    missing_comments.append(rule)
            else:
                not_accepted.append((rule, status or "<unset>"))
            references = _references_for_rule(entry)
            if not any(references.values()):
                missing_references.append(rule)
                continue
            if str(rule).startswith("docs-") and not references.get("docs"):
                missing_doc_references.append(str(rule))
            for refs in references.values():
                for reference in refs:
                    if not _reference_path_exists(root, reference):
                        bad_references.append((rule, reference))
            if rule == "docs-triggers" and references.get("docs"):
                incomplete_rule_docs.extend(
                    _validate_trigger_documentation(root, references["docs"])
                )

    if (
        missing
        or missing_official_rules
        or not_accepted
        or not_allowed_na
        or missing_references
        or missing_comments
        or bad_references
        or missing_doc_references
        or incomplete_rule_docs
        or rule_catalog_errors
    ):
        messages.append(
            f"Integration Quality Scale check failed for manifest level "
            f"{claimed_level!r}:\n"
        )
        if missing:
            messages.append("- Missing rule entries:")
            messages.extend(f"  - {rule}" for rule in missing)
        if missing_official_rules:
            messages.append("- Required official rules are missing from levels:")
            messages.extend(
                f"  - {level}: {rule}" for level, rule in missing_official_rules
            )
        if not_accepted:
            messages.append("- Rules not marked done:")
            messages.extend(f"  - {rule}: {status}" for rule, status in not_accepted)
        if not_allowed_na:
            messages.append("- Rules marked n/a without an allowlist exception:")
            messages.extend(f"  - {rule}" for rule in not_allowed_na)
        if missing_comments:
            messages.append("- n/a rules missing explanatory comments:")
            messages.extend(f"  - {rule}" for rule in missing_comments)
        if missing_references:
            messages.append("- Rules missing code/test/doc references:")
            messages.extend(f"  - {rule}" for rule in missing_references)
        if bad_references:
            messages.append("- Rule references that do not exist:")
            messages.extend(
                f"  - {rule}: {reference}" for rule, reference in bad_references
            )
        if missing_doc_references:
            messages.append("- Documentation rules missing docs references:")
            messages.extend(f"  - {rule}" for rule in missing_doc_references)
        if incomplete_rule_docs:
            messages.append("- Incomplete rule documentation:")
            messages.extend(f"  - {detail}" for detail in incomplete_rule_docs)
        if rule_catalog_errors:
            messages.append("- Official rule catalog metadata errors:")
            messages.extend(f"  - {detail}" for detail in rule_catalog_errors)
        return 1, messages

    brand_errors = _validate_brands_support(root, remote=validate_remote_brands)
    if brand_errors:
        return 1, brand_errors

    if claimed_level == "platinum":
        strict_typing_errors = _validate_strict_typing_contract(root)
        if strict_typing_errors:
            return 1, strict_typing_errors

    messages.append(
        f"Integration Quality Scale ({claimed_level}) OK: all claimed-level rules "
        "are documented."
    )
    return 0, messages


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--validate-remote-brands",
        action="store_true",
        help="Validate live home-assistant/brands GitHub and CDN assets.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    exit_code, messages = validate_quality_scale(
        root, validate_remote_brands=args.validate_remote_brands
    )
    output = sys.stderr if exit_code else sys.stdout
    print("\n".join(messages), file=output)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
