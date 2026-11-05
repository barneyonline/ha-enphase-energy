"""Regression coverage for Codecov's inventory and processed-delivery checks."""

from __future__ import annotations

from configparser import ConfigParser
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
import json
from pathlib import Path
import runpy
import os
import shlex
import subprocess
import sys
from urllib.error import HTTPError

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/codecov_reporting.py"
SPEC = spec_from_file_location("codecov_reporting", SCRIPT)
reporter = module_from_spec(SPEC)
SPEC.loader.exec_module(reporter)
SHA = "a" * 40
BUILD = "123.2"


def test_git_inventory_uses_exact_commit_without_producer_files_or_wrapper_artifacts(
    tmp_path,
):
    origin = tmp_path / "origin"
    origin.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Coverage Test",
        "GIT_AUTHOR_EMAIL": "coverage@example.test",
        "GIT_COMMITTER_NAME": "Coverage Test",
        "GIT_COMMITTER_EMAIL": "coverage@example.test",
    }

    def git(directory, *args):
        return subprocess.run(
            ["git", "-C", str(directory), *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git(origin, "init")
    # These files must remain metadata even when present in a producer commit.
    for name in (
        "custom_components/enphase_ev/example.py",
        "brand/icon@2x.png",
        "codecov",
        "codecov.yml",
        "setup.py",
        ".gitattributes",
    ):
        path = origin / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "producer content must not run or be read as executable configuration"
        )
    git(origin, "add", ".")
    git(origin, "commit", "-m", "Producer source")
    sha = git(origin, "rev-parse", "HEAD")
    git(origin, "config", "uploadpack.allowFilter", "true")
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/coverage-reporting.yml").read_text()
    )
    step = next(
        step
        for step in workflow["jobs"]["upload"]["steps"]
        if step.get("name")
        == "Prepare producer Git inventory without checking out producer files"
    )
    script = step["run"].replace(
        '"https://github.com/$GITHUB_REPOSITORY.git"', shlex.quote(origin.as_uri())
    )
    runner = tmp_path / "runner"
    runner.mkdir()
    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env={**env, "SOURCE_SHA": sha, "RUNNER_TEMP": str(runner)},
        check=True,
        capture_output=True,
        text=True,
    )
    source = runner / "codecov-source"
    assert git(source, "rev-parse", "HEAD") == sha
    assert git(source, "ls-files") == git(origin, "ls-files")
    assert git(source, "config", "core.hooksPath") == "/dev/null"
    assert {path.name for path in source.iterdir()} == {".git"}
    for name in ("codecov", "codecov.SHA256SUM", "codecov.SHA256SUM.sig"):
        (runner / "codecov-work" / name).write_text("wrapper download")
    assert git(source, "ls-files") == git(origin, "ls-files")
    assert not (source / "codecov").exists()


@pytest.fixture
def delivered():
    totals = {"files": 1, "lines": 2, "hits": 1, "misses": 1}
    # Faithful public API fields: per-file files=0, null build_code, null JUnit name/totals.
    commit = {
        "commitid": SHA,
        "state": "complete",
        "totals": totals,
        "report": {
            "files": [
                {
                    "name": "custom_components/enphase_ev/example.py",
                    "totals": {"files": 0, "lines": 2},
                }
            ]
        },
    }
    entries = [
        {
            "name": "coverage-123-2",
            "build_code": None,
            "state": "merged",
            "state_id": 6,
            "state_name": "MERGED",
            "totals": totals,
        },
        {
            "name": None,
            "job_code": "junit-123-2",
            "build_code": None,
            "state": "processed",
            "state_id": 1,
            "state_name": "UPLOADED",
            "totals": None,
        },
    ]
    return deepcopy(commit), deepcopy(entries)


def test_kind_specific_terminal_states_and_exact_attempt_attribution(delivered):
    commit, entries = delivered
    assert reporter.processed(commit, entries, SHA, BUILD)
    # Genuine missed lines are usable coverage; empty reports are different.
    commit["totals"].update(hits=0, misses=2)
    assert reporter.processed(commit, entries, SHA, BUILD)
    assert not reporter.processed(commit, entries, SHA, "123.3")
    entries[1]["job_code"] = "junit-123-1"
    assert not reporter.processed(commit, entries, SHA, BUILD)


@pytest.mark.parametrize(
    "target,state",
    [("coverage", "processed"), ("junit", "merged"), ("commit", "pending")],
)
def test_intermediate_or_wrong_kind_state_is_not_delivery(delivered, target, state):
    commit, entries = delivered
    (commit if target == "commit" else entries[target == "junit"])["state"] = state
    assert not reporter.processed(commit, entries, SHA, BUILD)


@pytest.mark.parametrize(
    "target,key,value,match",
    [
        ("commit", "commitid", "b" * 40, "another commit"),
        ("coverage", "state", "error", "unusable"),
        ("junit", "state", "error", "unusable"),
        ("coverage", "totals", None, "no measured"),
        ("coverage", "totals", {"files": 1, "lines": 0}, "no measured"),
        ("commit", "state", "error", "unusable"),
        ("commit", "totals", {"files": 0, "lines": 0}, "empty"),
        ("commit", "report", {"files": []}, "empty"),
    ],
)
def test_unusable_or_empty_processed_reports_fail(delivered, target, key, value, match):
    commit, entries = delivered
    (commit if target == "commit" else entries[target == "junit"])[key] = value
    with pytest.raises(ValueError, match=match):
        reporter.processed(commit, entries, SHA, BUILD)


def test_upload_pagination_uses_only_our_bounded_api_urls(monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return {
            "results": [len(calls)],
            "next": "https://untrusted.example" if len(calls) == 1 else None,
        }

    monkeypatch.setattr(reporter, "get_json", fetch)
    assert reporter.uploads(reporter.API) == [1, 2]
    assert all(url.startswith(reporter.API) for url in calls)
    monkeypatch.setattr(
        reporter, "get_json", lambda url: {"results": [], "next": "more"}
    )
    with pytest.raises(ValueError, match="pagination budget"):
        reporter.uploads(reporter.API)


def test_public_api_uses_no_credentials_and_has_a_response_budget(monkeypatch):
    def fetch(url, timeout):
        assert url == reporter.API
        assert timeout == 10
        return BytesIO(b'{"state":"complete"}')

    monkeypatch.setattr(reporter, "urlopen", fetch)
    assert reporter.get_json(reporter.API) == {"state": "complete"}
    monkeypatch.setattr(
        reporter,
        "urlopen",
        lambda *args, **kwargs: BytesIO(b"x" * (8 * 1024 * 1024 + 1)),
    )
    with pytest.raises(ValueError, match="response budget"):
        reporter.get_json(reporter.API)


def test_verify_waits_for_both_exact_attempt_uploads(delivered, monkeypatch):
    calls, sleeps = [], []

    def fetch(url):
        calls.append(url)
        if "uploads/" in url:
            return {
                "results": delivered[1] if len(calls) > 2 else delivered[1][:1],
                "next": None,
            }
        return delivered[0]

    monkeypatch.setattr(reporter, "get_json", fetch)
    monkeypatch.setattr(reporter.time, "sleep", sleeps.append)
    reporter.verify("owner/repo", SHA, BUILD)
    assert sleeps == [15]
    assert all(SHA in url for url in calls)


@pytest.mark.parametrize("code", [404, 503])
def test_api_processing_delay_is_bounded_and_other_errors_fail(monkeypatch, code):
    calls = []

    def fetch(url):
        calls.append(url)
        raise HTTPError(url, code, "failure", {}, None)

    monkeypatch.setattr(reporter, "get_json", fetch)
    monkeypatch.setattr(reporter.time, "sleep", lambda delay: None)
    with pytest.raises(TimeoutError if code == 404 else HTTPError):
        reporter.verify("owner/repo", SHA, BUILD)
    assert len(calls) == (12 if code == 404 else 1)


@pytest.mark.parametrize(
    "repository,sha,build",
    [("../repo", SHA, BUILD), ("owner/repo", "bad", BUILD), ("owner/repo", SHA, "0.1")],
)
def test_invalid_attribution_is_rejected(repository, sha, build):
    with pytest.raises(ValueError, match="Invalid"):
        reporter.verify(repository, sha, build)


def test_verification_command_line(delivered, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--sha", SHA, "--repository", "owner/repo", "--build", BUILD],
    )

    def fetch(url, timeout):
        payload = (
            {"results": delivered[1], "next": None}
            if "uploads/" in url
            else delivered[0]
        )
        return BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fetch)
    runpy.run_path(str(SCRIPT), run_name="__main__")


def test_documented_paths_and_isolated_uploader_settings():
    config = ConfigParser()
    config.read(ROOT / ".coveragerc")
    assert config.getboolean("run", "relative_files")
    assert config["run"]["source"] == "custom_components.enphase_ev"
    assert config["report"]["fail_under"] == "100"
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/coverage-reporting.yml").read_text()
    )
    assert workflow["permissions"] == {}
    steps = workflow["jobs"]["upload"]["steps"]
    checkout = next(
        step for step in steps if "actions/checkout@" in step.get("uses", "")
    )
    assert checkout["with"]["ref"] == "${{ github.workflow_sha }}"
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["sparse-checkout"] == "scripts/codecov_reporting.py"
    for step in steps:
        assert "actions/cache" not in step.get("uses", "")
        if "codecov/codecov-action@" in step.get("uses", ""):
            upload = step["with"]
            assert step["env"]["GIT_DIR"] == "${{ runner.temp }}/codecov-source/.git"
            assert step["env"]["GIT_WORK_TREE"] == "${{ runner.temp }}/codecov-source"
            assert upload["root_dir"] == "${{ runner.temp }}/codecov-source"
            assert upload["working-directory"] == "${{ runner.temp }}/codecov-work"
            assert upload["use_oidc"] is True
            assert upload["plugins"] == "noop"
            assert upload["disable_file_fixes"] is True
            assert upload["disable_search"] is True
            assert upload["fail_ci_if_error"] is True
            assert upload["override_commit"] == "${{ steps.source.outputs.sha }}"
            assert "run_attempt" in upload["override_build"]
    assert "always()" in steps[-1]["if"]
