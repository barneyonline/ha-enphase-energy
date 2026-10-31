"""Regression tests for the optimized GitHub Actions test workflow."""

from __future__ import annotations

from pathlib import Path
import json
import os
import runpy
import shlex
import subprocess
import sys

import pytest

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = "custom_components/enphase_ev/manifest.json"
CLASSIFIER = ROOT / "scripts/ci_manifest_metadata.py"
metadata_only = runpy.run_path(str(CLASSIFIER))["metadata_only"]


def _jobs() -> dict[str, object]:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    )
    return workflow["jobs"]


def test_slow_jobs_use_path_classification_instead_of_precommit_gate() -> None:
    jobs = _jobs()

    assert "changes" in jobs
    assert "translation-regressions" not in jobs
    for name in (
        "pytest",
        "python314-diagnostics",
        "quality-scale",
        "script-tests",
    ):
        job = jobs[name]
        assert job["needs"] == "changes"
        assert job["if"].startswith("${{ needs.changes.outputs.")


def test_path_classification_exposes_both_sides_of_renames() -> None:
    changes_job = _jobs()["changes"]
    classify_step = next(
        step
        for step in changes_job["steps"]
        if step.get("name") == "Classify changed paths"
    )

    assert "git diff --name-only --no-renames" in classify_step["run"]


def test_path_classification_when_base_advances_after_checkout(tmp_path):
    """Keep the merge base when main advances after the PR checkout was made."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Workflow Test",
        "GIT_AUTHOR_EMAIL": "workflow@example.test",
        "GIT_COMMITTER_NAME": "Workflow Test",
        "GIT_COMMITTER_EMAIL": "workflow@example.test",
    }

    def git(cwd, *args):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--initial-branch=main")
    git(origin, "commit", "--allow-empty", "-m", "Initial main")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", origin.as_uri(), str(checkout))
    git(checkout, "switch", "-c", "feature")
    source = checkout / "custom_components/enphase_ev/example.py"
    source.parent.mkdir(parents=True)
    source.write_text("# Integration change\n")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "Change integration")
    git(checkout, "switch", "main")
    git(checkout, "merge", "--no-ff", "feature", "-m", "PR merge")
    git(origin, "commit", "--allow-empty", "-m", "Advance main")

    step = next(
        step
        for step in _jobs()["changes"]["steps"]
        if step.get("name") == "Classify changed paths"
    )
    script = step["run"].replace("${{ github.event_name }}", "pull_request")
    script = script.replace("${{ github.base_ref }}", "main")
    output = tmp_path / "outputs"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=checkout,
        env={**env, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert set(output.read_text().splitlines()) == {
        "integration=true",
        "diagnostics=true",
        "quality=true",
        "scripts=false",
    }
    assert (
        git(checkout, "rev-parse", "--is-shallow-repository").stdout.strip() == "false"
    )


def test_pytest_reuses_one_coverage_run_and_loads_only_required_plugins() -> None:
    pytest_job = _jobs()["pytest"]
    steps = pytest_job["steps"]
    test_step = next(step for step in steps if step.get("id") == "pytest")
    coverage_step = next(
        step for step in steps if step.get("id") == "targeted_coverage"
    )

    assert test_step["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert "pytest_cov.plugin" in test_step["env"]["PYTEST_PLUGINS"]
    assert "tests/scripts" not in test_step["run"]
    assert "coverage run" not in coverage_step["run"]
    assert "check_statement_coverage.py" in coverage_step["run"]
    assert "--cov-branch" in test_step["run"]
    assert "--cov-report=json" in test_step["run"]


def test_uv_caches_depend_only_on_dependency_inputs() -> None:
    for job in _jobs().values():
        for step in job.get("steps", []):
            if not isinstance(step, dict) or "astral-sh/setup-uv@" not in str(
                step.get("uses", "")
            ):
                continue
            dependency_glob = step.get("with", {}).get("cache-dependency-glob", "")
            assert ".github/workflows" not in dependency_glob


def test_duplicate_static_gates_have_single_ci_owners() -> None:
    jobs = _jobs()
    precommit_step = next(
        step
        for step in jobs["pre-commit"]["steps"]
        if step.get("name") == "Run pre-commit"
    )
    quality_commands = [step.get("run", "") for step in jobs["quality-scale"]["steps"]]

    assert precommit_step["env"]["SKIP"] == "codespell,mypy-enphase-ev"
    assert (
        sum("validate_quality_scale.py" in command for command in quality_commands) == 1
    )
    assert (
        sum(command.lstrip().startswith("mypy ") for command in quality_commands) == 1
    )


@pytest.mark.parametrize(
    ("job_name", "output_name"),
    [("pytest", "integration"), ("python314-diagnostics", "diagnostics")],
)
def test_runtime_dependency_inputs_trigger_their_jobs(job_name, output_name, tmp_path):
    """Run each runtime job when any of its dependency inputs changes."""
    jobs = _jobs()
    setup_uv = next(
        step
        for step in jobs[job_name]["steps"]
        if "astral-sh/setup-uv@" in step.get("uses", "")
    )
    step = next(
        step
        for step in jobs["changes"]["steps"]
        if step.get("name") == "Classify changed paths"
    )
    script = step["run"]
    script = script[script.index("set_output()") :]
    output = tmp_path / "outputs"
    for path in shlex.split(setup_uv["with"]["cache-dependency-glob"]):
        output.unlink(missing_ok=True)
        env = {**os.environ, "GITHUB_OUTPUT": str(output), "changed_files": path}
        subprocess.run(
            ["bash", "-c", script], env=env, check=True, capture_output=True, text=True
        )
        assert f"{output_name}=true" in output.read_text().splitlines(), path


@pytest.fixture
def manifest_repo(tmp_path, monkeypatch):
    """Build a real PR checkout so classification exercises Git and JSON values."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Workflow Test",
        "GIT_AUTHOR_EMAIL": "workflow@example.test",
        "GIT_COMMITTER_NAME": "Workflow Test",
        "GIT_COMMITTER_EMAIL": "workflow@example.test",
    }

    def git(cwd, *args):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--initial-branch=main")
    manifest = origin / MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes((ROOT / MANIFEST).read_bytes())
    source = manifest.with_name("example.py")
    source.write_text("# Integration source\n")
    helper = origin / "scripts/ci_manifest_metadata.py"
    helper.parent.mkdir()
    helper.write_bytes(CLASSIFIER.read_bytes())
    git(origin, "add", ".")
    git(origin, "commit", "-m", "Initial main")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", origin.as_uri(), str(checkout))
    git(checkout, "switch", "-c", "feature")
    monkeypatch.chdir(checkout)

    def commit():
        git(checkout, "add", "-A")
        git(checkout, "commit", "-m", "PR change", "--allow-empty")

    def classify(event="pull_request"):
        step = next(
            step
            for step in _jobs()["changes"]["steps"]
            if step.get("name") == "Classify changed paths"
        )
        script = step["run"].replace("${{ github.event_name }}", event)
        script = script.replace("${{ github.base_ref }}", "main")
        output = tmp_path / "outputs"
        output.unlink(missing_ok=True)
        subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            cwd=checkout,
            env={**env, "GITHUB_OUTPUT": str(output)},
            check=True,
            capture_output=True,
            text=True,
        )
        return dict(line.split("=", 1) for line in output.read_text().splitlines())

    return checkout, origin, git, commit, classify


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("categories", ["energy"]),
        ("codeowners", ["@example"]),
        ("documentation", "https://example.test/docs"),
        ("issue_tracker", "https://example.test/issues"),
    ],
)
def test_descriptive_manifest_changes_skip_runtime_tests(manifest_repo, key, value):
    checkout, _, _, commit, classify = manifest_repo
    path = checkout / MANIFEST
    manifest = json.loads(path.read_text())
    manifest[key] = value
    path.write_text(json.dumps(manifest))
    (checkout / "CHANGELOG.md").write_text("Describe metadata change.\n")
    commit()

    assert metadata_only("origin/main") is True
    assert classify() == {
        "integration": "false",
        "diagnostics": "false",
        "quality": "true",
        "scripts": "false",
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("requirements", ["package==1.0"]),
        ("version", "99.0.0"),
        ("config_flow", False),
        ("config_flow", 1),
        ("dependencies", ["mqtt"]),
        ("after_dependencies", []),
        ("zeroconf", ["_example._tcp.local."]),
        ("unknown_future_key", True),
    ],
)
def test_runtime_manifest_fields_keep_full_suite(manifest_repo, key, value):
    checkout, _, _, commit, classify = manifest_repo
    path = checkout / MANIFEST
    manifest = json.loads(path.read_text())
    manifest.update({"categories": ["energy"], key: value})
    path.write_text(json.dumps(manifest))
    commit()

    assert metadata_only("origin/main") is False
    assert classify()["integration"] == "true"


@pytest.mark.parametrize("content", ["{", "[]", "null", "42", b"\xff"])
def test_invalid_manifests_keep_runtime_tests(manifest_repo, content):
    checkout, _, _, commit, classify = manifest_repo
    (checkout / MANIFEST).write_bytes(
        content if isinstance(content, bytes) else content.encode()
    )
    commit()

    assert metadata_only("origin/main") is False
    assert classify()["integration"] == "true"


@pytest.mark.parametrize("key,expected", [("documentation", False), ("version", True)])
def test_manifest_field_removal(manifest_repo, key, expected):
    checkout, _, _, commit, classify = manifest_repo
    path = checkout / MANIFEST
    manifest = json.loads(path.read_text())
    del manifest[key]
    path.write_text(json.dumps(manifest))
    commit()

    assert metadata_only("origin/main") is not expected
    assert classify()["integration"] == str(expected).lower()


@pytest.mark.parametrize("operation", ["add", "delete", "rename"])
def test_manifest_additions_deletions_and_renames_keep_tests(manifest_repo, operation):
    checkout, origin, git, commit, classify = manifest_repo
    path = checkout / MANIFEST
    if operation == "add":
        git(origin, "rm", MANIFEST)
        git(origin, "commit", "-m", "Remove base manifest")
        git(checkout, "fetch", "origin", "main")
        git(checkout, "reset", "--hard", "origin/main")
        path.write_bytes((ROOT / MANIFEST).read_bytes())
    elif operation == "delete":
        path.unlink()
    else:
        path.rename(path.with_name("old_manifest.json"))
    commit()

    assert metadata_only("origin/main") is False
    assert classify()["integration"] == "true"


@pytest.mark.parametrize(
    "path",
    [
        "custom_components/enphase_ev/example.py",
        "tests/components/enphase_ev/test_example.py",
        "custom_components/enphase_ev/strings.json",
        "custom_components/enphase_ev/translations/fr.json",
        "devtools/docker/requirements-dev.txt",
        ".coveragerc",
        ".github/workflows/tests.yml",
        "scripts/ci_manifest_metadata.py",
    ],
)
def test_metadata_with_other_runtime_changes_keeps_tests(manifest_repo, path):
    checkout, _, _, commit, classify = manifest_repo
    manifest_path = checkout / MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["categories"] = ["energy"]
    manifest_path.write_text(json.dumps(manifest))
    changed = checkout / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    with changed.open("a") as stream:
        stream.write("\n")
    commit()

    assert classify()["integration"] == "true"


def test_cross_group_source_rename_keeps_runtime_tests(manifest_repo):
    checkout, _, _, commit, classify = manifest_repo
    (checkout / "custom_components/enphase_ev/example.py").rename(
        checkout / "example.md"
    )
    commit()

    assert classify()["integration"] == "true"
    assert classify()["diagnostics"] == "true"


def test_manifest_comparison_uses_merge_base_when_main_advances(manifest_repo):
    checkout, origin, git, commit, classify = manifest_repo
    path = checkout / MANIFEST
    manifest = json.loads(path.read_text())
    manifest["requirements"] = ["package==1.0"]
    path.write_text(json.dumps(manifest))
    commit()
    # Main independently acquires the same runtime change after PR checkout.
    # Comparing branch tips would hide that change from the classifier.
    (origin / MANIFEST).write_text(json.dumps(manifest))
    git(origin, "add", ".")
    git(origin, "commit", "-m", "Advance main with same dependency")
    git(checkout, "fetch", "origin", "main")

    assert metadata_only("origin/main") is False
    assert classify()["integration"] == "true"


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_non_pr_events_keep_all_jobs(manifest_repo, event):
    *_, classify = manifest_repo
    assert set(classify(event).values()) == {"true"}


@pytest.mark.parametrize("base_ref,expected", [("origin/main", 0), ("missing", 1)])
def test_manifest_classifier_command_line(
    manifest_repo, monkeypatch, base_ref, expected
):
    monkeypatch.setattr(sys, "argv", [str(CLASSIFIER), "--base-ref", base_ref])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(CLASSIFIER), run_name="__main__")
    assert raised.value.code == expected
