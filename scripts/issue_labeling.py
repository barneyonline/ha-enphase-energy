"""Issue labeling helpers and GitHub workflow entrypoint."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib import error, parse, request

TITLE_PREFIX_TO_KIND = {
    "[Bug]:": "bug",
    "[Feature]:": "feature",
    "[Device support]:": "device_support",
}

NO_AREA_SELECTIONS = {
    "Other",
    "Other / unsure",
    "Other Enphase device or capability",
}

AREA_LABEL_MAP = {
    "Setup, sign-in, MFA, or reauthentication": "area/setup-auth",
    "IQ Gateway / System Controller": "area/gateway",
    "IQ Battery / Encharge / BatteryConfig": "area/battery",
    "IQ EV Charger / IQ EVSE": "area/ev-charger",
    "IQ Microinverters": "area/microinverters",
    "Heat Pump / HEMS device data": "area/hems",
    "Heat Pump / HEMS device": "area/hems",
    "Water Heater / site-energy channel": "area/water-heater",
    "Site or cloud energy telemetry": "area/site-energy",
    "Site / cloud telemetry capability": "area/site-energy",
    "Diagnostics, repairs, or service-availability reporting": "area/diagnostics",
    "Documentation": "area/docs",
    "Developer workflow / repository tooling": "area/tooling",
}

NO_SOURCE_SELECTIONS = {"Other"}

SOURCE_LABEL_MAP = {
    "HACS": "source/hacs",
    "Manual install in custom_components": "source/manual-install",
    "Development checkout": "source/dev-checkout",
}

BUG_DIAGNOSTICS_NEEDS_LABEL = {
    "Not yet, but I can upload them",
    "I cannot capture diagnostics",
}

DEVICE_DIAGNOSTICS_NEEDS_LABEL = {
    "Screenshots only",
    "Nothing attached yet",
}

LABEL_DEFINITIONS = {
    "type/device-support": {
        "color": "0E8A16",
        "description": "Requests support for a missing device, model, or capability",
    },
    "status/needs-triage": {
        "color": "FBCA04",
        "description": "New or reopened issue awaiting maintainer triage",
    },
    "status/needs-diagnostics": {
        "color": "D93F0B",
        "description": "More diagnostics or evidence are needed before triage can finish",
    },
    "area/setup-auth": {
        "color": "1D76DB",
        "description": "Setup, sign-in, MFA, or reauthentication",
    },
    "area/gateway": {
        "color": "1D76DB",
        "description": "IQ Gateway or System Controller behavior",
    },
    "area/battery": {
        "color": "1D76DB",
        "description": "IQ Battery, Encharge, or BatteryConfig behavior",
    },
    "area/ev-charger": {
        "color": "1D76DB",
        "description": "IQ EV Charger or IQ EVSE behavior",
    },
    "area/microinverters": {
        "color": "1D76DB",
        "description": "IQ Microinverter behavior",
    },
    "area/hems": {
        "color": "1D76DB",
        "description": "Heat pump or HEMS device data",
    },
    "area/water-heater": {
        "color": "1D76DB",
        "description": "Water heater or site-energy channel behavior",
    },
    "area/site-energy": {
        "color": "1D76DB",
        "description": "Site or cloud energy telemetry and presentation",
    },
    "area/diagnostics": {
        "color": "1D76DB",
        "description": "Diagnostics, repairs, or service-availability reporting",
    },
    "area/docs": {
        "color": "1D76DB",
        "description": "Documentation and setup guidance",
    },
    "area/tooling": {
        "color": "1D76DB",
        "description": "Developer workflow or repository tooling",
    },
    "source/hacs": {
        "color": "5319E7",
        "description": "Installed via HACS",
    },
    "source/manual-install": {
        "color": "5319E7",
        "description": "Installed manually in custom_components",
    },
    "source/dev-checkout": {
        "color": "5319E7",
        "description": "Running from a development checkout",
    },
}

MANAGED_PREFIXES = ("area/", "source/")
MANAGED_LABELS = set(LABEL_DEFINITIONS)
MANAGED_LABELS.update(
    {
        "type/device-support",
        "status/needs-diagnostics",
        "status/needs-triage",
    }
)

BODY_FIELD_LABELS = {
    "bug": {
        "area": "Primary affected area",
        "install_method": "Installation method",
        "diagnostics_status": "Diagnostics attached?",
    },
    "feature": {
        "area": "Area",
    },
    "device_support": {
        "area": "Device family",
        "diagnostics_status": "Evidence attached?",
    },
}


@dataclass(frozen=True)
class LabelDecision:
    """Desired label state for one issue."""

    desired_labels: set[str]
    managed_labels: set[str]
    to_add: list[str]
    to_remove: list[str]


def normalize(value: object) -> str:
    """Return a clean single-line string."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def issue_kind_from_title(title: str) -> str | None:
    """Infer issue kind from the title prefix."""
    for prefix, kind in TITLE_PREFIX_TO_KIND.items():
        if title.startswith(prefix):
            return kind
    return None


def parse_issue_form_section(body: str | None, heading: str) -> str:
    """Extract the first logical line below a rendered issue form heading."""
    if not body:
        return ""

    pattern = rf"(?ms)^### {re.escape(heading)}\s*\n(.*?)(?=^### |\Z)"
    match = re.search(pattern, body)
    if not match:
        return ""

    block = match.group(1)
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or line == "_No response_":
            continue
        if line.startswith("<!--"):
            continue
        return normalize(line.lstrip("- ").strip())
    return ""


def _mapped_area_label(value: str) -> str | None:
    normalized = normalize(value)
    if not normalized or normalized in NO_AREA_SELECTIONS:
        return None
    return AREA_LABEL_MAP.get(normalized)


def _mapped_source_label(value: str) -> str | None:
    normalized = normalize(value)
    if not normalized or normalized in NO_SOURCE_SELECTIONS:
        return None
    return SOURCE_LABEL_MAP.get(normalized)


def _resolved_field(
    parser_value: str,
    body_value: str,
    resolver: Any,
) -> str:
    """Prefer parser output if it is recognized, otherwise fall back to body text."""
    parser_text = normalize(parser_value)
    if parser_text and (
        resolver(parser_text) is not None or parser_text in NO_AREA_SELECTIONS
    ):
        return parser_text

    body_text = normalize(body_value)
    if body_text:
        return body_text

    return parser_text


def _current_label_names(issue: dict[str, Any]) -> list[str]:
    labels = issue.get("labels") or []
    names: list[str] = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
            continue
        name = label.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def desired_labels_for_issue(
    issue: dict[str, Any],
    *,
    action: str,
    parser_outputs: dict[str, str] | None = None,
) -> LabelDecision:
    """Compute desired managed labels for an issue."""
    parser_outputs = parser_outputs or {}
    kind = issue_kind_from_title(normalize(issue.get("title")))
    if kind is None:
        return LabelDecision(set(), set(), [], [])

    current_labels = _current_label_names(issue)
    if {"automation/hal", "automation/control"}.intersection(current_labels):
        return LabelDecision(set(), set(), [], [])
    desired_labels: set[str] = set()

    if kind == "device_support":
        desired_labels.add("type/device-support")

    if action in {"opened", "reopened"} or "status/needs-triage" in current_labels:
        desired_labels.add("status/needs-triage")

    body = issue.get("body") or ""
    body_fields = BODY_FIELD_LABELS[kind]

    area_value = _resolved_field(
        parser_outputs.get(
            (
                "primary_area"
                if kind == "bug"
                else "area" if kind == "feature" else "device_family"
            ),
            "",
        ),
        parse_issue_form_section(body, body_fields["area"]),
        _mapped_area_label,
    )
    area_label = _mapped_area_label(area_value)
    if area_label:
        desired_labels.add(area_label)

    if kind == "bug":
        install_method = _resolved_field(
            parser_outputs.get("install_method", ""),
            parse_issue_form_section(body, body_fields["install_method"]),
            _mapped_source_label,
        )
        source_label = _mapped_source_label(install_method)
        if source_label:
            desired_labels.add(source_label)

    diagnostics_heading = body_fields.get("diagnostics_status")
    diagnostics_value = _resolved_field(
        parser_outputs.get("diagnostics_status", ""),
        (
            parse_issue_form_section(body, diagnostics_heading)
            if diagnostics_heading
            else ""
        ),
        lambda value: value,
    )
    if kind == "bug" and diagnostics_value in BUG_DIAGNOSTICS_NEEDS_LABEL:
        desired_labels.add("status/needs-diagnostics")
    if kind == "device_support" and diagnostics_value in DEVICE_DIAGNOSTICS_NEEDS_LABEL:
        desired_labels.add("status/needs-diagnostics")

    managed_current = [
        label
        for label in current_labels
        if label in MANAGED_LABELS
        or any(label.startswith(prefix) for prefix in MANAGED_PREFIXES)
    ]
    to_add = sorted(label for label in desired_labels if label not in current_labels)
    to_remove = sorted(
        label for label in managed_current if label not in desired_labels
    )
    return LabelDecision(desired_labels, set(managed_current), to_add, to_remove)


def extract_dropdown_metadata(template_path: Path) -> dict[str, dict[str, Any]]:
    """Extract dropdown labels and options without third-party YAML parsers."""
    dropdowns: dict[str, dict[str, Any]] = {}
    lines = template_path.read_text(encoding="utf-8").splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]
        if line.strip() != "- type: dropdown":
            index += 1
            continue

        dropdown_indent = len(line) - len(line.lstrip(" "))
        index += 1
        field_id = ""
        field_label = ""

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(" "))

            if stripped.startswith("- type:") and indent == dropdown_indent:
                break

            if stripped.startswith("id:"):
                field_id = stripped.split(":", 1)[1].strip()

            if stripped.startswith("label:") and field_id:
                field_label = stripped.split(":", 1)[1].strip()

            if stripped == "options:" and field_id:
                option_indent = indent
                index += 1
                options: list[str] = []
                while index < len(lines):
                    option_line = lines[index]
                    option_stripped = option_line.strip()
                    option_line_indent = len(option_line) - len(option_line.lstrip(" "))

                    if option_line_indent <= option_indent:
                        break

                    if option_stripped.startswith("- "):
                        options.append(option_stripped[2:].strip())

                    index += 1
                dropdowns[field_id] = {
                    "label": field_label,
                    "options": options,
                }
                continue

            index += 1

    return dropdowns


def validate_template_mappings(repo_root: Path) -> list[str]:
    """Return validation errors for issue template dropdown mappings."""
    template_dir = repo_root / ".github" / "ISSUE_TEMPLATE"
    expected = {
        template_dir
        / "bug_report.yml": {
            "labels": {
                "primary_area": BODY_FIELD_LABELS["bug"]["area"],
                "install_method": BODY_FIELD_LABELS["bug"]["install_method"],
            },
            "primary_area": set(AREA_LABEL_MAP) | NO_AREA_SELECTIONS,
            "install_method": set(SOURCE_LABEL_MAP) | NO_SOURCE_SELECTIONS,
        },
        template_dir
        / "feature_request.yml": {
            "labels": {
                "area": BODY_FIELD_LABELS["feature"]["area"],
            },
            "area": set(AREA_LABEL_MAP) | NO_AREA_SELECTIONS,
        },
        template_dir
        / "device_support_request.yml": {
            "labels": {
                "device_family": BODY_FIELD_LABELS["device_support"]["area"],
            },
            "device_family": set(AREA_LABEL_MAP) | NO_AREA_SELECTIONS,
        },
    }

    errors: list[str] = []
    for template_path, required_fields in expected.items():
        dropdowns = extract_dropdown_metadata(template_path)
        expected_labels = required_fields.get("labels", {})
        for field_id, expected_label in expected_labels.items():
            metadata = dropdowns.get(field_id)
            actual_label = normalize(metadata.get("label") if metadata else "")
            if actual_label != expected_label:
                errors.append(
                    f"{template_path.name}:{field_id} label mismatch: expected '{expected_label}', got '{actual_label or '<missing>'}'"
                )

        for field_id, allowed_options in required_fields.items():
            if field_id == "labels":
                continue
            metadata = dropdowns.get(field_id)
            actual = set(metadata.get("options", []) if metadata else [])
            if not actual:
                errors.append(f"{template_path.name}:{field_id} dropdown not found")
                continue
            missing = sorted(actual - allowed_options)
            if missing:
                errors.append(
                    f"{template_path.name}:{field_id} has unmapped options: {', '.join(missing)}"
                )
    return errors


def _github_api_request(
    token: str,
    method: str,
    url: str,
    data: dict[str, Any] | None = None,
) -> Any:
    payload = None if data is None else json.dumps(data).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "ha-enphase-ev-issue-labeling",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with request.urlopen(req) as response:
        if response.status == 204:
            return None
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read().decode(charset)
        return json.loads(raw) if raw else None


def _list_target_issues(
    token: str, repo: str, issue_number: str | None
) -> list[dict[str, Any]]:
    base_url = f"https://api.github.com/repos/{repo}"
    if issue_number:
        issue = _github_api_request(token, "GET", f"{base_url}/issues/{issue_number}")
        return (
            [issue]
            if "pull_request" not in issue
            and issue_kind_from_title(normalize(issue.get("title")))
            else []
        )

    issues: list[dict[str, Any]] = []
    page = 1
    while True:
        page_issues = _github_api_request(
            token,
            "GET",
            f"{base_url}/issues?state=open&per_page=100&page={page}",
        )
        if not page_issues:
            return issues
        for issue in page_issues:
            if "pull_request" in issue:
                continue
            if issue_kind_from_title(normalize(issue.get("title"))):
                issues.append(issue)
        page += 1


def _ensure_labels(token: str, repo: str, label_names: set[str]) -> None:
    base_url = f"https://api.github.com/repos/{repo}"
    for label_name in sorted(label_names):
        definition = LABEL_DEFINITIONS.get(label_name)
        if definition is None:
            continue
        encoded = parse.quote(label_name, safe="")
        try:
            _github_api_request(token, "GET", f"{base_url}/labels/{encoded}")
        except error.HTTPError as exc:
            if exc.code != 404:
                raise
            _github_api_request(
                token,
                "POST",
                f"{base_url}/labels",
                {"name": label_name, **definition},
            )


def _reconcile_issue(
    token: str,
    repo: str,
    issue: dict[str, Any],
    *,
    action: str,
    parser_outputs: dict[str, str] | None = None,
) -> LabelDecision:
    decision = desired_labels_for_issue(
        issue, action=action, parser_outputs=parser_outputs
    )
    if not decision.to_add and not decision.to_remove:
        return decision

    base_url = f"https://api.github.com/repos/{repo}"
    _ensure_labels(token, repo, set(decision.to_add))

    if decision.to_add:
        _github_api_request(
            token,
            "POST",
            f"{base_url}/issues/{issue['number']}/labels",
            {"labels": decision.to_add},
        )

    for label_name in decision.to_remove:
        encoded = parse.quote(label_name, safe="")
        try:
            _github_api_request(
                token,
                "DELETE",
                f"{base_url}/issues/{issue['number']}/labels/{encoded}",
            )
        except error.HTTPError as exc:
            if exc.code != 404:
                raise

    return decision


def _parser_outputs_from_env() -> dict[str, str]:
    return {
        "primary_area": os.getenv("PRIMARY_AREA", ""),
        "area": os.getenv("FEATURE_AREA", ""),
        "device_family": os.getenv("DEVICE_FAMILY", ""),
        "install_method": os.getenv("INSTALL_METHOD", ""),
        "diagnostics_status": os.getenv("DIAGNOSTICS_STATUS", ""),
    }


def main() -> int:
    """Reconcile labels for issue events or workflow_dispatch backfills."""
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    event_path = os.getenv("GITHUB_EVENT_PATH", "")

    if not token or not repo:
        print("GITHUB_TOKEN and GITHUB_REPOSITORY are required", file=sys.stderr)
        return 1

    if event_name == "issues":
        if not event_path:
            print("GITHUB_EVENT_PATH is required for issue events", file=sys.stderr)
            return 1
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
        issue = payload.get("issue") or {}
        action = normalize(payload.get("action"))
        if issue_kind_from_title(normalize(issue.get("title"))) is None:
            return 0
        _reconcile_issue(
            token,
            repo,
            issue,
            action=action,
            parser_outputs=_parser_outputs_from_env(),
        )
        return 0

    if event_name == "workflow_dispatch":
        issue_number = normalize(os.getenv("WORKFLOW_DISPATCH_ISSUE_NUMBER"))
        for issue in _list_target_issues(token, repo, issue_number or None):
            _reconcile_issue(token, repo, issue, action="workflow_dispatch")
        return 0

    print(f"Unsupported event: {event_name}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
