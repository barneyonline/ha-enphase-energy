"""Exercise source inventories, path matching and real processed-delivery gates."""

from __future__ import annotations

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
import json
from pathlib import Path
import runpy
import subprocess
import sys
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/codecov_reporting.py"
SPEC = spec_from_file_location("codecov_reporting", SCRIPT)
reporter = module_from_spec(SPEC)
SPEC.loader.exec_module(reporter)
SHA = "a" * 40
BUILD = "123.2"
SOURCE = reporter.PREFIX + "example.py"
XML = f"""<coverage lines-valid="2" lines-covered="1">
<sources><source>/home/runner/work/ha-enphase-energy/ha-enphase-energy</source></sources>
<packages><package><classes><class filename="{SOURCE}"><lines>
<line number="1" hits="1" branch="true" condition-coverage="50% (1/2)"/>
<line number="2" hits="0"/>
</lines></class></classes></package></packages></coverage>"""


@pytest.fixture
def inputs(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "coverage.xml").write_text(XML)
    (reports / "junit.xml").write_text("<testsuites tests='2'/>")
    tree = {
        "source_sha": SHA,
        "truncated": False,
        "tree": [
            {"path": path, "type": "blob", "mode": "100644"}
            for path in (SOURCE, "tests/test_example.py", ".github/workflows/tests.yml")
        ],
    }
    tree_file = tmp_path / "tree.json"
    tree_file.write_text(json.dumps(tree))
    return tree, tree_file, reports, tmp_path / "inventory"


def test_inventory_maps_covered_and_uncovered_lines_without_source_programs(inputs):
    tree, tree_file, reports, inventory = inputs
    # A report-only directory reproduces the discarded network from the regression.
    with pytest.raises(ValueError, match="no unique source match"):
        reporter.coverage_paths(reports / "coverage.xml", {"coverage.xml", "junit.xml"})
    reporter.prepare(tree_file, reports, inventory, SHA)
    assert {
        path.relative_to(inventory).as_posix()
        for path in inventory.rglob("*")
        if path.is_file()
    } == {item["path"] for item in tree["tree"]}
    assert all(
        path.stat().st_size == 0 and not path.stat().st_mode & 0o111
        for path in inventory.rglob("*")
        if path.is_file()
    )
    assert not (inventory / ".git").exists()
    root = ET.parse(reports / "coverage.xml").getroot()
    assert [item.text for item in root.findall("./sources/source")] == ["."]
    assert root.find(".//class").attrib["filename"] == SOURCE
    assert [line.attrib for line in root.findall(".//line")] == [
        {
            "number": "1",
            "hits": "1",
            "branch": "true",
            "condition-coverage": "50% (1/2)",
        },
        {"number": "2", "hits": "0"},
    ]
    with pytest.raises(FileExistsError):
        reporter.prepare(tree_file, reports, inventory, SHA)


@pytest.mark.parametrize("budget", ["MAX_REPORT", "MAX_REPORTS"])
def test_normalization_preserves_report_budgets(inputs, monkeypatch, budget):
    _, tree_file, reports, inventory = inputs
    monkeypatch.setattr(reporter, budget, 1)
    with pytest.raises(ValueError, match="Normalized report budget"):
        reporter.prepare(tree_file, reports, inventory, SHA)
    assert not inventory.exists()
    assert (reports / "coverage.xml").read_text() == XML


def test_inventory_never_materializes_symlinks_or_submodules(inputs):
    tree, _, _, _ = inputs
    tree["tree"] += [
        {"path": "link", "type": "blob", "mode": "120000"},
        {"path": "submodule", "type": "commit", "mode": "160000"},
        {"path": "directory", "type": "tree", "mode": "040000"},
        {"path": "executable", "type": "blob", "mode": "100755"},
    ]
    assert reporter.source_paths(tree, SHA) == {
        SOURCE,
        "tests/test_example.py",
        ".github/workflows/tests.yml",
        "executable",
    }


def test_real_repository_tree_names_are_supported():
    """Include actual @2x brand assets in the source inventory."""
    output = subprocess.run(
        ["git", "ls-tree", "-rz", "--full-tree", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    tree = []
    for entry in output.rstrip("\0").split("\0"):
        metadata, path = entry.split("\t", 1)
        mode, kind, _ = metadata.split(" ")
        tree.append({"path": path, "mode": mode, "type": kind})
    paths = reporter.source_paths(
        {"tree": tree, "source_sha": SHA, "truncated": False}, SHA
    )
    assert reporter.PREFIX + "brand/icon@2x.png" in paths


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "a/../b",
        "a//b",
        ".git/config",
        "a/.git/hooks/x",
        "./file",
        "a\\b",
        "a\nfile",
        "a" * 1025,
        "",
    ],
)
def test_reject_unsafe_paths(path):
    with pytest.raises(ValueError, match="Unsafe"):
        reporter.safe_path(path)


@pytest.mark.parametrize(
    "change,match",
    [
        ({"source_sha": "b" * 40}, "another commit"),
        ({"truncated": True}, "truncated"),
        ({"tree": []}, "budget"),
        ({"tree": [{}] * 10001}, "budget"),
        (
            {"tree": [{"path": "elsewhere.py", "type": "blob", "mode": "100644"}]},
            "no integration",
        ),
        (
            {"tree": [{"path": SOURCE, "type": "blob", "mode": "100644"}] * 2},
            "Duplicate",
        ),
    ],
)
def test_reject_unverified_or_incomplete_inventory(inputs, change, match):
    tree, _, _, _ = inputs
    tree.update(change)
    with pytest.raises(ValueError, match=match):
        reporter.source_paths(tree, SHA)


def test_normalize_package_relative_coverage_and_missing_sources(inputs):
    _, _, reports, _ = inputs
    report = reports / "coverage.xml"
    report.write_text(
        XML.replace(SOURCE, "example.py").replace(
            XML[XML.index("<sources>") : XML.index("</sources>") + 10], ""
        )
    )
    tree, expected = reporter.coverage_paths(report, {SOURCE})
    assert tree.find(".//source").text == "."
    assert tree.find(".//class").attrib["filename"] == SOURCE
    assert expected == {SOURCE}


@pytest.mark.parametrize(
    "xml,paths,match",
    [
        ("<!DOCTYPE coverage><coverage/>", {SOURCE}, "declarations"),
        ("<!ENTITY fake 'value'><coverage/>", {SOURCE}, "declarations"),
        ("<testsuites/>", {SOURCE}, "Cobertura"),
        ("<coverage/>", {SOURCE}, "no measured"),
        (XML.replace(SOURCE, "example.py"), {SOURCE, "example.py"}, "unique"),
        (XML.replace(SOURCE, "elsewhere.py"), {"elsewhere.py"}, "outside"),
        (
            XML.replace(
                "</classes>",
                XML[XML.index("<class filename") : XML.index("</class>") + 8]
                + "</classes>",
            ),
            {SOURCE},
            "duplicated",
        ),
        (
            XML.replace(
                '<line number="1" hits="1" branch="true" condition-coverage="50% (1/2)"/>',
                "",
            ).replace('<line number="2" hits="0"/>', ""),
            {SOURCE},
            "no measured",
        ),
    ],
)
def test_reject_unusable_or_ambiguous_coverage(inputs, xml, paths, match):
    report = inputs[2] / "coverage.xml"
    report.write_text(xml)
    with pytest.raises(ValueError, match=match):
        reporter.coverage_paths(report, paths)


@pytest.fixture
def delivered():
    totals = {"files": 1, "lines": 2, "hits": 1, "misses": 1, "sessions": 1}
    commit = {
        "commitid": SHA,
        "state": "complete",
        "totals": totals,
        "report": {"files": [{"name": SOURCE, "totals": totals}]},
    }
    # Codecov's per-file totals use files=0 even when that file has measured lines.
    commit["report"]["files"][0]["totals"] = {**totals, "files": 0}
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
            "build_code": None,
            "state": "processed",
            "state_name": "UPLOADED",
            "job_code": "junit-123-2",
            "totals": None,
            "storage_path": "test_results/v1/raw/result.xml",
        },
    ]
    return deepcopy(commit), deepcopy(entries)


def test_processed_requires_project_coverage_and_separate_junit(delivered):
    commit, entries = delivered
    assert reporter.processed(commit, entries, SHA, BUILD, {SOURCE})
    # A fully missed report is usable; a 0% number alone is not an ingestion error.
    commit["totals"].update(hits=0, misses=2)
    assert reporter.processed(commit, entries, SHA, BUILD, {SOURCE})
    # Green no-op patch status and stale sessions never establish delivery.
    assert not reporter.processed(commit, entries, SHA, "123.3", {SOURCE})
    assert not reporter.processed(commit, [], SHA, BUILD, {SOURCE})


def test_junit_is_attributed_by_job_code_with_null_name_and_totals(delivered):
    commit, entries = delivered
    assert entries[1]["name"] is None
    assert entries[1]["totals"] is None
    assert all(entry["build_code"] is None for entry in entries)
    assert reporter.processed(commit, entries, SHA, BUILD, {SOURCE})
    entries[1]["job_code"] = "junit-123-1"
    assert not reporter.processed(commit, entries, SHA, BUILD, {SOURCE})


@pytest.mark.parametrize("target,state", [(0, "processed"), (1, "merged")])
def test_upload_kinds_require_their_own_terminal_states(delivered, target, state):
    commit, entries = delivered
    entries[target]["state"] = state
    assert not reporter.processed(commit, entries, SHA, BUILD, {SOURCE})


@pytest.mark.parametrize(
    "target,key,value,match",
    [
        ("commit", "commitid", "b" * 40, "another commit"),
        ("coverage", "state", "error", "unusable"),
        ("junit", "state", "error", "unusable"),
        ("coverage", "totals", None, "no source lines"),
        ("coverage", "totals", {"files": 1, "lines": 0}, "no source lines"),
        ("junit", "storage_path", "v4/raw/coverage", "JUnit"),
        ("commit", "state", "error", "unusable"),
        ("commit", "totals", {"files": 0, "lines": 0}, "empty"),
        ("commit", "totals", {"files": 1, "lines": 2, "sessions": 0}, "empty"),
        ("commit", "report", {"files": []}, "missing measured"),
        (
            "commit",
            "report",
            {"files": [{"name": SOURCE, "totals": {"files": 1, "lines": 0}}]},
            "missing measured",
        ),
    ],
)
def test_reject_processed_errors_or_empty_reports(delivered, target, key, value, match):
    commit, entries = delivered
    item = commit if target == "commit" else entries[target == "junit"]
    item[key] = value
    with pytest.raises(ValueError, match=match):
        reporter.processed(commit, entries, SHA, BUILD, {SOURCE})


@pytest.mark.parametrize("target", ["commit", "coverage", "junit"])
def test_pending_is_not_processed(delivered, target):
    commit, entries = delivered
    (commit if target == "commit" else entries[target == "junit"])["state"] = "pending"
    assert not reporter.processed(commit, entries, SHA, BUILD, {SOURCE})


def test_no_expected_lines_cannot_pass(delivered):
    with pytest.raises(ValueError, match="missing measured"):
        reporter.processed(*delivered, SHA, BUILD, set())


def test_upload_pagination_is_bounded_and_does_not_follow_external_urls(monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return {
            "results": [{"page": len(calls)}],
            "next": "https://untrusted.example/" if len(calls) == 1 else None,
        }

    monkeypatch.setattr(reporter, "get_json", fetch)
    assert reporter.uploads(reporter.API + "owner/repos/repo/commits/sha/") == [
        {"page": 1},
        {"page": 2},
    ]
    assert all(url.startswith(reporter.API) for url in calls)
    monkeypatch.setattr(
        reporter, "get_json", lambda url: {"results": [], "next": "more"}
    )
    with pytest.raises(ValueError, match="pagination budget"):
        reporter.uploads(reporter.API)


def test_public_api_has_no_auth_and_bounded_responses(monkeypatch):
    url = reporter.API + "owner/repos/repo/commits/sha/?format=json"

    def fetch(request, timeout):
        assert not any(key.lower() == "authorization" for key in request.headers)
        assert timeout == 10
        response = BytesIO(b'{"state":"complete"}')
        response.geturl = lambda: url
        return response

    monkeypatch.setattr(reporter, "urlopen", fetch)
    assert reporter.get_json(url) == {"state": "complete"}
    with pytest.raises(ValueError, match="origin"):
        reporter.get_json("https://untrusted.example/")
    monkeypatch.setattr(reporter, "MAX_RESPONSE", 1)
    with pytest.raises(ValueError, match="response budget"):
        reporter.get_json(url)
    with pytest.raises(ValueError, match="redirect"):
        reporter.get_json(url + "unexpected")


def test_bounded_verification_waits_for_both_attempt_uploads(
    inputs, delivered, monkeypatch
):
    commit, entries = delivered
    calls, sleeps = [], []

    def fetch(url):
        calls.append(url)
        if "uploads/" in url:
            return {"results": entries if len(calls) > 2 else entries[:1], "next": None}
        return commit

    monkeypatch.setattr(reporter, "get_json", fetch)
    monkeypatch.setattr(reporter.time, "sleep", sleeps.append)
    reporter.verify(
        "barneyonline/ha-enphase-energy", SHA, BUILD, inputs[2] / "coverage.xml"
    )
    assert sleeps == [15]
    assert all(SHA in url for url in calls)


@pytest.mark.parametrize(
    "error",
    [
        HTTPError(reporter.API, 404, "not yet", {}, None),
        HTTPError(reporter.API, 503, "unavailable", {}, None),
        URLError("temporary"),
        TimeoutError("temporary"),
    ],
)
def test_transient_errors_get_at_most_two_retries(
    inputs, delivered, monkeypatch, error
):
    calls, sleeps = [], []

    def fetch(url):
        calls.append(url)
        if len(calls) <= 2:
            raise error
        return (
            {"results": delivered[1], "next": None}
            if "uploads/" in url
            else delivered[0]
        )

    monkeypatch.setattr(reporter, "get_json", fetch)
    monkeypatch.setattr(reporter.time, "sleep", sleeps.append)
    reporter.verify(
        "barneyonline/ha-enphase-energy", SHA, BUILD, inputs[2] / "coverage.xml"
    )
    assert sleeps == [15, 15]


@pytest.mark.parametrize("code,attempts", [(401, 1), (429, 3), (503, 3), (404, 12)])
def test_api_failures_remain_visible(inputs, monkeypatch, code, attempts):
    calls = []

    def fetch(url):
        calls.append(url)
        raise HTTPError(url, code, "failure", {}, None)

    monkeypatch.setattr(reporter, "get_json", fetch)
    monkeypatch.setattr(reporter.time, "sleep", lambda delay: None)
    with pytest.raises(TimeoutError if code == 404 else HTTPError):
        reporter.verify("owner/repo", SHA, BUILD, inputs[2] / "coverage.xml")
    assert len(calls) == attempts


def test_processing_timeout_is_a_failure(inputs, monkeypatch):
    monkeypatch.setattr(
        reporter, "get_json", lambda url: {"commitid": SHA, "state": "pending"}
    )
    monkeypatch.setattr(reporter, "uploads", lambda base: [])
    monkeypatch.setattr(reporter.time, "sleep", lambda delay: None)
    with pytest.raises(TimeoutError, match="polling budget"):
        reporter.verify("owner/repo", SHA, BUILD, inputs[2] / "coverage.xml")


@pytest.mark.parametrize(
    "repository,sha,build",
    [
        ("../repo", SHA, BUILD),
        ("owner/repo", "bad", BUILD),
        ("owner/repo", SHA, "1.inject"),
        ("owner/repo", SHA, "0.1"),
    ],
)
def test_verification_rejects_invalid_attribution(inputs, repository, sha, build):
    with pytest.raises(ValueError, match="Invalid"):
        reporter.verify(repository, sha, build, inputs[2] / "coverage.xml")


def test_command_line_prepare_and_verify(inputs, delivered, monkeypatch):
    _, tree_file, reports, inventory = inputs
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "prepare",
            "--sha",
            SHA,
            "--reports",
            str(reports),
            "--tree",
            str(tree_file),
            "--inventory",
            str(inventory),
        ],
    )
    runpy.run_path(str(SCRIPT), run_name="__main__")
    assert (inventory / SOURCE).is_file()
    calls = []
    monkeypatch.setattr(reporter, "verify", lambda *args: calls.append(args))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "verify",
            "--sha",
            SHA,
            "--reports",
            str(reports),
            "--repository",
            "owner/repo",
            "--build",
            BUILD,
        ],
    )
    reporter.main()
    assert calls == [("owner/repo", SHA, BUILD, reports / "coverage.xml")]


def test_reporter_uses_only_trusted_helper_and_preserves_upload_controls():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/coverage-reporting.yml").read_text()
    )
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
            config = step["with"]
            assert (
                config["root_dir"]
                == config["working-directory"]
                == "${{ runner.temp }}/codecov-source"
            )
            assert config["use_oidc"] is True
            assert config["plugins"] == "noop"
            assert config["disable_file_fixes"] is True
            assert config["disable_search"] is True
            assert config["fail_ci_if_error"] is True
            assert config["override_commit"] == "${{ steps.source.outputs.sha }}"
            assert "run_attempt" in config["override_build"]
    verification = steps[-1]
    assert "always()" in verification["if"]
    assert "steps.junit_upload.outcome == 'success'" in verification["if"]
    assert "python3 -I -B" in verification["run"]
    assert workflow["permissions"] == {}
