"""Exercise report-file boundaries used by automatic and manual reporting."""

from pathlib import Path
import os
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow():
    return yaml.safe_load(
        (ROOT / ".github/workflows/coverage-reporting.yml").read_text()
    )


def test_manual_validation_preserves_permissions_and_trusted_data_flow():
    config = workflow()
    # PyYAML's YAML 1.1 loader interprets the Actions "on" key as True.
    events = config[True]
    assert events["workflow_run"] == {"workflows": ["Tests"], "types": ["completed"]}
    inputs = events["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "producer_run_id",
        "producer_run_attempt",
        "pull_request_number",
        "expected_head_sha",
    }
    assert all(
        item["required"] and item["type"] == "string" for item in inputs.values()
    )
    assert config["permissions"] == {}
    job = config["jobs"]["upload"]
    assert job["permissions"] == {
        "contents": "read",
        "actions": "read",
        "pull-requests": "read",
        "id-token": "write",
    }
    steps = job["steps"]
    by_name = {step["name"]: step for step in steps}
    checkout = by_name["Check out only the trusted reporter helper"]["with"]
    assert checkout["ref"] == (
        "${{ github.event_name == 'workflow_dispatch' && "
        "github.event.repository.default_branch || github.workflow_sha }}"
    )
    assert checkout["sparse-checkout"] == "scripts/codecov_reporting.py"
    assert checkout["persist-credentials"] is False
    download = by_name["Download only the selected producer reports"]["with"]
    assert download["artifact-ids"] == "${{ steps.source.outputs.artifact_id }}"
    assert download["run-id"] == "${{ steps.source.outputs.run_id }}"
    names = list(by_name)
    assert names.index("Resolve report artifact and source attribution") < names.index(
        "Check out only the trusted reporter helper"
    )
    assert names.index("Recheck producer and PR before uploading") < names.index(
        "Upload coverage to Codecov"
    )
    for name in ("Upload coverage to Codecov", "Upload test results to Codecov"):
        step = by_name[name]
        assert step["uses"] == (
            "codecov/codecov-action@303a32d7a59b442fa8d48b6a1cc6825c09c847a5"
        )
        assert step["with"]["override_commit"] == "${{ steps.source.outputs.sha }}"
        assert step["with"]["override_build"] == (
            "${{ steps.source.outputs.run_id }}.${{ steps.source.outputs.run_attempt }}"
        )
        assert step["with"]["use_oidc"] is True
        assert step["with"]["disable_search"] is True
        assert step["with"]["disable_file_fixes"] is True


def validate_reports(root):
    step = next(
        item
        for item in workflow()["jobs"]["upload"]["steps"]
        if item["name"] == "Validate report files"
    )
    lines = step["run"].splitlines()
    assert lines[0] == "python3 - <<'PYTHON'" and lines[-1] == "PYTHON"
    return subprocess.run(
        [sys.executable, "-c", "\n".join(lines[1:-1])],
        env={**os.environ, "REPORTS": str(root)},
        capture_output=True,
        text=True,
    )


@pytest.fixture
def reports(tmp_path):
    (tmp_path / "coverage.xml").write_text("<coverage/>\n")
    (tmp_path / "junit.xml").write_text("<testsuites/>\n")
    return tmp_path


def test_report_files_accept_only_expected_regular_files(reports):
    assert validate_reports(reports).returncode == 0
    (reports / "coverage.json").write_text("{}\n")
    assert validate_reports(reports).returncode == 0


@pytest.mark.parametrize(
    "case", ["missing", "unexpected", "nested", "symlink", "hardlink", "empty", "large"]
)
def test_report_files_reject_untrusted_structure_and_budgets(reports, case):
    coverage = reports / "coverage.xml"
    if case == "missing":
        coverage.unlink()
    elif case == "unexpected":
        (reports / "producer.py").write_text("raise RuntimeError('must never execute')")
    elif case == "nested":
        (reports / "nested").mkdir()
        coverage.rename(reports / "nested/coverage.xml")
    elif case == "symlink":
        coverage.unlink()
        coverage.symlink_to(reports / "junit.xml")
    elif case == "hardlink":
        coverage.unlink()
        coverage.hardlink_to(reports / "junit.xml")
    elif case == "empty":
        coverage.write_text("")
    elif case == "large":
        with coverage.open("r+b") as stream:
            stream.truncate(16777217)
    result = validate_reports(reports)
    assert result.returncode != 0, case
    assert "must never execute" not in result.stderr


def test_report_files_reject_aggregate_budget(reports):
    for filename in ("coverage.xml", "junit.xml", "coverage.json"):
        with (reports / filename).open("wb") as stream:
            stream.truncate(12 * 1024 * 1024)
    result = validate_reports(reports)
    assert result.returncode != 0
    assert "aggregate budget exceeded" in result.stderr
