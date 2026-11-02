from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from urllib import error

import pytest


def _load_module():
    root = Path(__file__).resolve().parents[2]
    module_path = root / "scripts" / "issue_labeling.py"
    spec = importlib.util.spec_from_file_location("issue_labeling", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


issue_labeling = _load_module()


def _issue(title: str, body: str, labels: list[str] | None = None) -> dict[str, object]:
    return {
        "number": 42,
        "title": title,
        "body": body,
        "labels": [{"name": label} for label in (labels or [])],
    }


def test_bug_issue_uses_parser_area_install_method_and_diagnostics() -> None:
    decision = issue_labeling.desired_labels_for_issue(
        _issue(
            "[Bug]: Charger controls fail",
            "### Primary affected area\n\nOther / unsure\n",
            labels=["bug", "status/needs-triage"],
        ),
        action="opened",
        parser_outputs={
            "primary_area": "IQ EV Charger / IQ EVSE",
            "install_method": "HACS",
            "diagnostics_status": "Not yet, but I can upload them",
        },
    )

    assert decision.desired_labels == {
        "area/ev-charger",
        "source/hacs",
        "status/needs-diagnostics",
        "status/needs-triage",
    }
    assert decision.to_add == [
        "area/ev-charger",
        "source/hacs",
        "status/needs-diagnostics",
    ]
    assert decision.to_remove == []


def test_feature_issue_falls_back_to_raw_body_when_parser_area_missing() -> None:
    body = """
### Area

Documentation

### Problem statement

Current docs are incomplete.
""".strip()

    decision = issue_labeling.desired_labels_for_issue(
        _issue(
            "[Feature]: Improve docs",
            body,
            labels=["enhancement", "status/needs-triage"],
        ),
        action="edited",
        parser_outputs={"area": ""},
    )

    assert decision.desired_labels == {"area/docs", "status/needs-triage"}
    assert decision.to_add == ["area/docs"]
    assert decision.to_remove == []


def test_device_support_issue_falls_back_when_parser_value_is_unmapped() -> None:
    body = """
### Device family

Heat Pump / HEMS device

### Evidence attached?

Nothing attached yet
""".strip()

    decision = issue_labeling.desired_labels_for_issue(
        _issue("[Device support]: Add heat pump", body, labels=["enhancement"]),
        action="reopened",
        parser_outputs={
            "device_family": "Unexpected parser output",
            "diagnostics_status": "",
        },
    )

    assert decision.desired_labels == {
        "area/hems",
        "status/needs-diagnostics",
        "status/needs-triage",
        "type/device-support",
    }


def test_bug_issue_other_selections_do_not_add_area_or_source_labels() -> None:
    body = """
### Installation method

Other

### Primary affected area

Other / unsure

### Diagnostics attached?

Yes, both config-entry and device diagnostics attached
""".strip()

    decision = issue_labeling.desired_labels_for_issue(
        _issue("[Bug]: Intermittent error", body, labels=["status/needs-triage"]),
        action="edited",
        parser_outputs={},
    )

    assert decision.desired_labels == {"status/needs-triage"}
    assert decision.to_add == []
    assert decision.to_remove == []


def test_bug_diagnostics_states_are_mapped_correctly() -> None:
    body = """
### Diagnostics attached?

I cannot capture diagnostics
""".strip()
    with_label = issue_labeling.desired_labels_for_issue(
        _issue("[Bug]: Auth loop", body),
        action="opened",
        parser_outputs={},
    )
    assert "status/needs-diagnostics" in with_label.desired_labels

    without_label = issue_labeling.desired_labels_for_issue(
        _issue(
            "[Bug]: Auth loop",
            "### Diagnostics attached?\n\nYes, config-entry diagnostics attached",
        ),
        action="opened",
        parser_outputs={},
    )
    assert "status/needs-diagnostics" not in without_label.desired_labels


def test_bug_install_method_source_labels_are_mapped_correctly() -> None:
    expectations = {
        "HACS": "source/hacs",
        "Manual install in custom_components": "source/manual-install",
        "Development checkout": "source/dev-checkout",
    }

    for install_method, expected_label in expectations.items():
        decision = issue_labeling.desired_labels_for_issue(
            _issue(
                "[Bug]: Setup fails",
                "### Diagnostics attached?\n\nYes, config-entry diagnostics attached",
            ),
            action="opened",
            parser_outputs={"install_method": install_method},
        )
        assert expected_label in decision.desired_labels


def test_template_dropdown_options_are_fully_mapped() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    errors = issue_labeling.validate_template_mappings(repo_root)
    assert errors == []


def test_parse_helpers_cover_empty_and_comment_only_sections() -> None:
    assert issue_labeling.normalize(None) == ""
    assert issue_labeling.issue_kind_from_title("[Question]: Help") is None
    assert issue_labeling.parse_issue_form_section(None, "Area") == ""
    assert (
        issue_labeling.parse_issue_form_section("### Area\n\n_No response_", "Area")
        == ""
    )
    assert (
        issue_labeling.parse_issue_form_section(
            "### Area\n\n<!-- comment -->\n- Documentation",
            "Area",
        )
        == "Documentation"
    )


def test_unknown_issue_kind_returns_empty_decision() -> None:
    decision = issue_labeling.desired_labels_for_issue(
        _issue("[Question]: Help", "### Area\n\nDocumentation"),
        action="opened",
        parser_outputs={},
    )
    assert decision.desired_labels == set()
    assert decision.to_add == []
    assert decision.to_remove == []


def test_managed_labels_are_removed_when_no_longer_desired() -> None:
    decision = issue_labeling.desired_labels_for_issue(
        {
            "number": 42,
            "title": "[Feature]: Improve docs",
            "body": "### Area\n\nDocumentation",
            "labels": [
                "status/needs-triage",
                "area/gateway",
                "source/hacs",
                {"name": "enhancement"},
            ],
        },
        action="edited",
        parser_outputs={},
    )
    assert decision.to_add == ["area/docs"]
    assert decision.to_remove == ["area/gateway", "source/hacs"]
    assert decision.managed_labels == {
        "area/gateway",
        "source/hacs",
        "status/needs-triage",
    }


def test_validate_template_mappings_reports_missing_dropdowns(tmp_path: Path) -> None:
    template_dir = tmp_path / ".github" / "ISSUE_TEMPLATE"
    template_dir.mkdir(parents=True)
    (template_dir / "bug_report.yml").write_text(
        "body:\n  - type: dropdown\n    id: install_method\n    attributes:\n      label: Installation method\n      options:\n        - HACS\n",
        encoding="utf-8",
    )
    (template_dir / "feature_request.yml").write_text("body: []\n", encoding="utf-8")
    (template_dir / "device_support_request.yml").write_text(
        "body:\n  - type: dropdown\n    id: device_family\n    attributes:\n      label: Device family\n      options:\n        - Unknown capability\n",
        encoding="utf-8",
    )

    errors = issue_labeling.validate_template_mappings(tmp_path)

    assert "bug_report.yml:primary_area dropdown not found" in errors
    assert "feature_request.yml:area dropdown not found" in errors
    assert any(
        "device_support_request.yml:device_family has unmapped options" in item
        for item in errors
    )


def test_validate_template_mappings_reports_label_mismatches(tmp_path: Path) -> None:
    template_dir = tmp_path / ".github" / "ISSUE_TEMPLATE"
    template_dir.mkdir(parents=True)
    (template_dir / "bug_report.yml").write_text(
        "body:\n  - type: dropdown\n    id: primary_area\n    attributes:\n      label: Affected area\n      options:\n        - IQ EV Charger / IQ EVSE\n  - type: dropdown\n    id: install_method\n    attributes:\n      label: Installation method\n      options:\n        - HACS\n",
        encoding="utf-8",
    )
    (template_dir / "feature_request.yml").write_text(
        "body:\n  - type: dropdown\n    id: area\n    attributes:\n      label: Area\n      options:\n        - Documentation\n",
        encoding="utf-8",
    )
    (template_dir / "device_support_request.yml").write_text(
        "body:\n  - type: dropdown\n    id: device_family\n    attributes:\n      label: Device family\n      options:\n        - IQ EV Charger / IQ EVSE\n",
        encoding="utf-8",
    )

    errors = issue_labeling.validate_template_mappings(tmp_path)

    assert (
        "bug_report.yml:primary_area label mismatch: expected 'Primary affected area', got 'Affected area'"
        in errors
    )


def test_list_target_issues_filters_pull_requests_and_non_form_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        [
            {"number": 1, "title": "[Bug]: One"},
            {"number": 2, "title": "[Question]: Two"},
            {"number": 3, "title": "[Feature]: Three", "pull_request": {"url": "x"}},
        ],
        [],
    ]

    def fake_request(token: str, method: str, url: str, data=None):
        assert token == "token"
        assert method == "GET"
        assert data is None
        return responses.pop(0)

    monkeypatch.setattr(issue_labeling, "_github_api_request", fake_request)

    issues = issue_labeling._list_target_issues("token", "owner/repo", None)

    assert issues == [{"number": 1, "title": "[Bug]: One"}]


def test_list_target_issues_returns_requested_issue_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        issue_labeling,
        "_github_api_request",
        lambda token, method, url, data=None: {
            "number": 99,
            "title": "[Feature]: Docs",
        },
    )

    issues = issue_labeling._list_target_issues("token", "owner/repo", "99")

    assert issues == [{"number": 99, "title": "[Feature]: Docs"}]


def test_ensure_labels_creates_missing_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str, object]] = []

    def fake_request(token: str, method: str, url: str, data=None):
        calls.append((token, method, url, data))
        if method == "GET" and url.endswith("/labels/source%2Fhacs"):
            raise error.HTTPError(url, 404, "missing", None, None)
        return None

    monkeypatch.setattr(issue_labeling, "_github_api_request", fake_request)

    issue_labeling._ensure_labels("token", "owner/repo", {"source/hacs", "unknown"})

    assert calls[0][1] == "GET"
    assert calls[1][1] == "POST"
    assert calls[1][3]["name"] == "source/hacs"


def test_ensure_labels_reraises_non_404_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(token: str, method: str, url: str, data=None):
        raise error.HTTPError(url, 500, "boom", None, None)

    monkeypatch.setattr(issue_labeling, "_github_api_request", fake_request)

    with pytest.raises(error.HTTPError):
        issue_labeling._ensure_labels("token", "owner/repo", {"source/hacs"})


def test_reconcile_issue_adds_and_removes_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensured: list[set[str]] = []
    calls: list[tuple[str, str, str, object]] = []

    monkeypatch.setattr(
        issue_labeling,
        "_ensure_labels",
        lambda token, repo, names: ensured.append(names),
    )

    def fake_request(token: str, method: str, url: str, data=None):
        calls.append((token, method, url, data))
        if method == "DELETE" and url.endswith("/labels/source%2Fhacs"):
            raise error.HTTPError(url, 404, "missing", None, None)
        return None

    monkeypatch.setattr(issue_labeling, "_github_api_request", fake_request)

    issue = {
        "number": 42,
        "title": "[Feature]: Improve docs",
        "body": "### Area\n\nDocumentation",
        "labels": ["status/needs-triage", "source/hacs"],
    }

    decision = issue_labeling._reconcile_issue(
        "token", "owner/repo", issue, action="edited"
    )

    assert ensured == [{"area/docs"}]
    assert decision.to_add == ["area/docs"]
    assert decision.to_remove == ["source/hacs"]
    assert calls[0][1] == "POST"
    assert calls[1][1] == "DELETE"


def test_reconcile_issue_returns_early_when_no_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensured: list[set[str]] = []
    monkeypatch.setattr(
        issue_labeling,
        "_ensure_labels",
        lambda token, repo, names: ensured.append(names),
    )
    monkeypatch.setattr(
        issue_labeling,
        "_github_api_request",
        lambda token, method, url, data=None: pytest.fail("unexpected API call"),
    )

    issue = {
        "number": 42,
        "title": "[Feature]: Improve docs",
        "body": "### Area\n\nDocumentation",
        "labels": ["status/needs-triage", "area/docs"],
    }

    decision = issue_labeling._reconcile_issue(
        "token", "owner/repo", issue, action="edited"
    )

    assert decision.to_add == []
    assert decision.to_remove == []
    assert ensured == []


def test_reconcile_issue_reraises_delete_errors_other_than_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        issue_labeling, "_ensure_labels", lambda token, repo, names: None
    )

    def fake_request(token: str, method: str, url: str, data=None):
        if method == "DELETE":
            raise error.HTTPError(url, 500, "boom", None, None)
        return None

    monkeypatch.setattr(issue_labeling, "_github_api_request", fake_request)

    issue = {
        "number": 42,
        "title": "[Feature]: Improve docs",
        "body": "### Area\n\nDocumentation",
        "labels": ["status/needs-triage", "source/hacs"],
    }

    with pytest.raises(error.HTTPError):
        issue_labeling._reconcile_issue("token", "owner/repo", issue, action="edited")


def test_parser_outputs_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIMARY_AREA", "IQ Gateway / System Controller")
    monkeypatch.setenv("FEATURE_AREA", "Documentation")
    monkeypatch.setenv("DEVICE_FAMILY", "IQ EV Charger / IQ EVSE")
    monkeypatch.setenv("INSTALL_METHOD", "HACS")
    monkeypatch.setenv("DIAGNOSTICS_STATUS", "Nothing attached yet")

    assert issue_labeling._parser_outputs_from_env() == {
        "primary_area": "IQ Gateway / System Controller",
        "area": "Documentation",
        "device_family": "IQ EV Charger / IQ EVSE",
        "install_method": "HACS",
        "diagnostics_status": "Nothing attached yet",
    }


def test_main_handles_missing_required_environment(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    assert issue_labeling.main() == 1
    assert "GITHUB_TOKEN and GITHUB_REPOSITORY are required" in capsys.readouterr().err


def test_main_handles_issue_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(
        '{"action":"opened","issue":{"number":7,"title":"[Bug]: Demo","body":"### Diagnostics attached?\\n\\nYes, config-entry diagnostics attached","labels":[]}}',
        encoding="utf-8",
    )

    recorded: list[tuple[str, str, dict[str, object], str, dict[str, str]]] = []

    def fake_reconcile(
        token: str,
        repo: str,
        issue: dict[str, object],
        *,
        action: str,
        parser_outputs=None,
    ):
        recorded.append((token, repo, issue, action, parser_outputs or {}))
        return None

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("PRIMARY_AREA", "IQ Gateway / System Controller")
    monkeypatch.setattr(issue_labeling, "_reconcile_issue", fake_reconcile)

    assert issue_labeling.main() == 0
    assert recorded[0][3] == "opened"
    assert recorded[0][4]["primary_area"] == "IQ Gateway / System Controller"


def test_main_handles_issue_events_without_event_path(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)

    assert issue_labeling.main() == 1
    assert "GITHUB_EVENT_PATH is required for issue events" in capsys.readouterr().err


def test_main_skips_non_form_issue_titles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(
        '{"action":"opened","issue":{"number":7,"title":"[Question]: Demo","body":"","labels":[]}}',
        encoding="utf-8",
    )

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setattr(
        issue_labeling,
        "_reconcile_issue",
        lambda *args, **kwargs: pytest.fail("unexpected reconcile"),
    )

    assert issue_labeling.main() == 0


def test_main_handles_workflow_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched = [{"number": 1, "title": "[Feature]: Docs"}]
    reconciled: list[tuple[dict[str, object], str]] = []

    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("WORKFLOW_DISPATCH_ISSUE_NUMBER", "15")
    monkeypatch.setattr(
        issue_labeling,
        "_list_target_issues",
        lambda token, repo, issue_number: dispatched,
    )
    monkeypatch.setattr(
        issue_labeling,
        "_reconcile_issue",
        lambda token, repo, issue, *, action, parser_outputs=None: reconciled.append(
            (issue, action)
        ),
    )

    assert issue_labeling.main() == 0
    assert reconciled == [(dispatched[0], "workflow_dispatch")]


def test_main_rejects_unsupported_events(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")

    assert issue_labeling.main() == 1
    assert "Unsupported event: push" in capsys.readouterr().err


def test_github_api_request_serializes_and_decodes_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeHeaders:
        @staticmethod
        def get_content_charset():
            return "utf-8"

    class FakeResponse:
        status = 200
        headers = FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return b'{"ok": true}'

    def fake_urlopen(req):
        captured["full_url"] = req.full_url
        captured["method"] = req.get_method()
        captured["data"] = req.data
        return FakeResponse()

    monkeypatch.setattr(issue_labeling.request, "urlopen", fake_urlopen)

    response = issue_labeling._github_api_request(
        "token",
        "POST",
        "https://api.github.com/example",
        {"name": "value"},
    )

    assert response == {"ok": True}
    assert captured["full_url"] == "https://api.github.com/example"
    assert captured["method"] == "POST"
    assert captured["data"] == b'{"name": "value"}'


def test_github_api_request_handles_empty_204_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHeaders:
        @staticmethod
        def get_content_charset():
            return "utf-8"

    class FakeResponse:
        status = 204
        headers = FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return b""

    monkeypatch.setattr(issue_labeling.request, "urlopen", lambda req: FakeResponse())

    assert (
        issue_labeling._github_api_request(
            "token",
            "DELETE",
            "https://api.github.com/example",
        )
        is None
    )


def test_hal_issues_keep_lifecycle_and_area_labels() -> None:
    for action in ("opened", "edited", "workflow_dispatch"):
        for managed in ("automation/hal",):
            for objects in (False, True):
                labels = [managed, "status/awaiting-release", "area/climate"]
                issue = {
                    "number": 42,
                    "title": "[Bug]: Already under investigation",
                    "body": "### Primary affected area\n\nDocumentation",
                    "labels": (
                        [{"name": name} for name in labels] if objects else labels
                    ),
                }
                decision = issue_labeling.desired_labels_for_issue(
                    issue, action=action, parser_outputs={}
                )
                assert decision.to_add == []
                assert decision.to_remove == []
                assert issue["labels"] == (
                    [{"name": name} for name in labels] if objects else labels
                )


def test_manual_reconciliation_can_remove_only_a_stale_area(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(issue_labeling, "_ensure_labels", lambda *args: None)
    monkeypatch.setattr(
        issue_labeling,
        "_github_api_request",
        lambda token, method, url, data=None: calls.append((method, url, data)),
    )
    issue = {
        "number": 42,
        "title": "[Feature]: Improve docs",
        "body": "### Area\n\nDocumentation",
        "labels": [{"name": "area/docs"}, {"name": "area/climate"}, {"name": None}],
    }
    decision = issue_labeling._reconcile_issue(
        "token", "owner/repo", issue, action="workflow_dispatch"
    )
    assert decision.to_add == []
    assert decision.to_remove == ["area/climate"]
    assert calls == [
        (
            "DELETE",
            "https://api.github.com/repos/owner/repo/issues/42/labels/area%2Fclimate",
            None,
        )
    ]


def test_dropdown_options_ignore_indented_comments(tmp_path: Path) -> None:
    template = tmp_path / "form.yml"
    template.write_text(
        "- type: dropdown\n  id: area\n  attributes:\n    label: Area\n"
        "    options:\n      - Documentation\n      # Additional area\n      - Energy\n",
        encoding="utf-8",
    )
    assert issue_labeling.extract_dropdown_metadata(template) == {
        "area": {"label": "Area", "options": ["Documentation", "Energy"]}
    }


def test_manual_target_cannot_select_a_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        issue_labeling,
        "_github_api_request",
        lambda *args: {
            "number": 43,
            "title": "[Bug]: Pull request",
            "pull_request": {},
            "labels": [{"name": "area/climate"}],
        },
    )
    assert issue_labeling._list_target_issues("token", "owner/repo", "43") == []
