"""Regression tests for the optimized GitHub Actions test workflow."""

from __future__ import annotations

from pathlib import Path

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
        "homeassistant-2026-8",
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
    assert "coverage report" in coverage_step["run"]


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
