"""Regression tests for the optimized GitHub Actions test workflow."""

from __future__ import annotations

from pathlib import Path
import os
import shlex
import subprocess

import pytest

import yaml

ROOT = Path(__file__).resolve().parents[2]


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
        "minimum-homeassistant",
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
        if not isinstance(job, dict):
            continue
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


def _compatibility_test_paths():
    paths = set()
    for step in _jobs()["minimum-homeassistant"]["steps"]:
        for token in shlex.split(step.get("run", "")):
            if token.startswith("tests/"):
                paths.add(token.split("::")[0])
    return sorted(paths)


@pytest.mark.parametrize(
    "path",
    [
        *_compatibility_test_paths(),
        "hacs.json",
        "devtools/docker/requirements-min-ha.txt",
        "devtools/docker/constraints-min-ha.txt",
    ],
)
def test_every_compatibility_input_triggers_its_lane(path, tmp_path):
    """Execute the classifier for minimum-version metadata, dependencies, and tests."""
    step = next(
        step
        for step in _jobs()["changes"]["steps"]
        if step.get("name") == "Classify changed paths"
    )
    script = step["run"]
    # The classifier below consumes git's changed-path output. No network/git
    # mutation is needed to exercise its actual shell expressions.
    script = script[script.index("          set_output()".strip()) :]
    output = tmp_path / "outputs"
    env = {**os.environ, "GITHUB_OUTPUT": str(output), "changed_files": path}
    subprocess.run(
        ["bash", "-c", script], env=env, check=True, capture_output=True, text=True
    )
    assert "compatibility=true" in output.read_text().splitlines()
