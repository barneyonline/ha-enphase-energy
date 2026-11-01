"""Validate CI report provenance and bounded data; never execute candidate code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

REPOSITORIES = {
    "barneyonline/ha-groq": 1233471561,
    "barneyonline/ha-enphase-energy": 1052048747,
    "barneyonline/ha-energy-planner": 1288905007,
}
WORKFLOWS = {".github/workflows/tests.yml", ".github/workflows/ci.yml"}
PRODUCERS = {
    "barneyonline/ha-groq": (
        "checks",
        "Run tests with coverage in Home Assistant Docker",
    ),
    "barneyonline/ha-enphase-energy": ("pytest", "Run tests"),
    "barneyonline/ha-energy-planner": (
        "pytest-coverage",
        "Run tests with coverage in Home Assistant Docker",
    ),
}


class Refused(ValueError):
    """A fixed decision code without supplied payloads or credentials."""


def require(condition: object, code: str) -> None:
    if not condition:
        raise Refused(code)


def positive(value: object) -> bool:
    return type(value) is int and 0 < value < 2**63


def safe_branch(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value.encode("utf-8")) <= 1024
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def timestamp(value: object) -> datetime:
    require(
        isinstance(value, str)
        and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
        ),
        "timestamp_refused",
    )
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def producer_executed(
    repository: str, run_id: int, attempt: int, read: Callable[[str], Any]
) -> bool:
    job_name, step_name = PRODUCERS[repository]
    inventory = read(
        f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
    )
    count, jobs = inventory.get("total_count"), inventory.get("jobs")
    require(
        type(count) is int
        and 0 <= count < 100
        and isinstance(jobs, list)
        and len(jobs) == count,
        "job_inventory_refused",
    )
    selected = [j for j in jobs if j.get("name") in {job_name, "checks / " + job_name}]
    require(len(selected) == 1, "producer_job_refused")
    job = selected[0]
    require(job.get("status") == "completed", "producer_job_incomplete")
    if job.get("conclusion") == "skipped":
        return False
    steps = [s for s in job.get("steps", []) if s.get("name") == step_name]
    require(len(steps) <= 1, "producer_step_refused")
    if not steps:
        require(
            job.get("conclusion") in {"failure", "cancelled", "timed_out"},
            "producer_step_missing",
        )
        return False
    step = steps[0]
    require(step.get("status") == "completed", "producer_step_incomplete")
    require(
        step.get("conclusion") in {"success", "failure", "skipped", "cancelled"},
        "producer_step_refused",
    )
    return step["conclusion"] in {"success", "failure"}


def verify_scope(
    event: dict[str, Any], repository: str, read: Callable[[str], Any]
) -> dict[str, Any]:
    """Re-read first-party scope; event names, branch text and reports grant nothing."""
    require(repository in REPOSITORIES, "repository_refused")
    expected_id = REPOSITORIES[repository]
    require(
        event.get("repository", {}).get("id") == expected_id, "event_repository_refused"
    )
    supplied = event.get("workflow_run", {})
    run_id, attempt = supplied.get("id"), supplied.get("run_attempt")
    require(positive(run_id) and positive(attempt), "run_identity_refused")
    prefix = f"repos/{repository}"
    run = read(f"{prefix}/actions/runs/{run_id}")
    require(
        run.get("id") == run_id
        and run.get("run_attempt") == attempt
        and run.get("repository", {}).get("id") == expected_id
        and run.get("head_repository", {}).get("id") == expected_id,
        "run_scope_changed",
    )
    require(
        run.get("status") == "completed"
        and run.get("conclusion") in {"success", "failure"},
        "run_not_complete",
    )
    require(
        run.get("event") in {"pull_request", "push", "workflow_dispatch"},
        "run_event_refused",
    )
    path = run.get("path", "").split("@", 1)[0]
    require(path in WORKFLOWS, "producer_path_refused")
    workflow = read(f"{prefix}/actions/workflows/{path.rsplit('/', 1)[1]}")
    require(
        positive(workflow.get("id"))
        and workflow["id"] == run.get("workflow_id")
        and workflow.get("path") == path
        and workflow.get("state") == "active",
        "producer_identity_refused",
    )
    head = run.get("head_sha")
    require(
        isinstance(head, str) and re.fullmatch(r"[a-f0-9]{40}", head), "head_refused"
    )
    candidates = read(f"{prefix}/commits/{head}/pulls?per_page=100")
    require(
        isinstance(candidates, list) and len(candidates) < 100, "pr_inventory_refused"
    )
    matching = [
        p
        for p in candidates
        if p.get("head", {}).get("sha") == head
        and p.get("head", {}).get("repo", {}).get("id") == expected_id
        and p.get("base", {}).get("repo", {}).get("id") == expected_id
        and p.get("state") == "open"
    ]
    require(len(matching) <= 1, "ambiguous_pr_scope")
    pr_number = 0
    branch = run.get("head_branch")
    if matching:
        number = matching[0].get("number")
        require(positive(number), "pr_identity_refused")
        pr = read(f"{prefix}/pulls/{number}")
        require(
            pr.get("number") == number
            and pr.get("state") == "open"
            and pr.get("head", {}).get("sha") == head
            and pr.get("head", {}).get("repo", {}).get("id") == expected_id
            and pr.get("base", {}).get("repo", {}).get("id") == expected_id,
            "pr_scope_changed",
        )
        pr_number, branch = number, pr["head"].get("ref")
    else:
        require(
            run["event"] != "pull_request"
            and isinstance(branch, str)
            and (branch == "main" or re.fullmatch(r"release/[0-9]+\.[0-9]+", branch)),
            "unassociated_run_refused",
        )
    require(safe_branch(branch), "branch_output_refused")
    result = {
        "repository": repository,
        "run_id": run_id,
        "run_attempt": attempt,
        "head_sha": head,
        "branch": branch,
        "pr_number": pr_number,
    }
    if not producer_executed(repository, run_id, attempt, read):
        return {**result, "eligible": False}
    inventory = read(f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100")
    count, artifacts = inventory.get("total_count"), inventory.get("artifacts")
    require(
        type(count) is int
        and 0 <= count < 100
        and isinstance(artifacts, list)
        and len(artifacts) == count,
        "artifact_inventory_refused",
    )
    selected = [a for a in artifacts if a.get("name") == f"coverage-evidence-{attempt}"]
    require(len(selected) == 1, "artifact_identity_refused")
    artifact = selected[0]
    require(
        timestamp(run.get("run_started_at"))
        <= timestamp(artifact.get("created_at"))
        <= timestamp(run.get("updated_at")),
        "artifact_attempt_refused",
    )
    origin = artifact.get("workflow_run", {})
    require(
        positive(artifact.get("id"))
        and artifact.get("expired") is False
        and type(artifact.get("size_in_bytes")) is int
        and 0 < artifact["size_in_bytes"] <= 33_554_432
        and origin.get("id") == run_id
        and origin.get("repository_id") == expected_id
        and origin.get("head_repository_id") == expected_id
        and origin.get("head_sha") == head,
        "artifact_scope_refused",
    )
    return {**result, "eligible": True, "artifact_id": artifact["id"]}


def validate_reports(directory: Path) -> dict[str, Any]:
    """Parse only expected regular bounded files; report strings stay data."""
    require(
        directory.is_absolute()
        and directory.resolve() == directory
        and directory.is_dir(),
        "report_directory_refused",
    )
    allowed, present, total = {"coverage.xml", "junit.xml", "coverage.json"}, set(), 0
    for path in directory.iterdir():
        require(
            path.name in allowed and len(present) < len(allowed), "report_name_refused"
        )
        info = path.lstat()
        require(
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "report_file_refused"
        )
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and 0 < info.st_size <= 16_777_216,
                "report_file_refused",
            )
            total += info.st_size
            require(total <= 33_554_432, "report_budget_exceeded")
            raw = stream.read(16_777_217)
            require(len(raw) == info.st_size, "report_changed")
        if path.suffix == ".xml":
            require(b"\0" not in raw, "xml_encoding_refused")
            text = raw.decode("utf-8")
            require(
                "<!DOCTYPE" not in text.upper() and "<!ENTITY" not in text.upper(),
                "xml_entity_refused",
            )
            tag = ET.fromstring(text).tag
            require(
                tag
                in (
                    {"coverage"}
                    if path.name == "coverage.xml"
                    else {"testsuites", "testsuite"}
                ),
                "xml_root_refused",
            )
        else:
            require(isinstance(json.loads(raw), dict), "coverage_json_refused")
        present.add(path.name)
    require({"coverage.xml", "junit.xml"} <= present, "required_reports_missing")
    return {"coverage": True, "junit": True, "total_bytes": total}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise Refused("api_redirect_refused")


class ReadAPI:
    def __init__(self, repository: str) -> None:
        require(repository in REPOSITORIES, "repository_refused")
        self.repository = repository
        self.token = os.environ.get("GH_TOKEN", "")
        require(bool(self.token), "read_token_required")
        self.opener = urllib.request.build_opener(NoRedirect())

    def __call__(self, route: str) -> Any:
        prefix = re.escape(f"repos/{self.repository}/")
        require(
            re.fullmatch(
                prefix
                + r"(?:actions/runs/[1-9][0-9]*(?:/artifacts\?per_page=100|/attempts/[1-9][0-9]*/jobs\?per_page=100)?|actions/workflows/(?:tests|ci)\.yml|commits/[a-f0-9]{40}/pulls\?per_page=100|pulls/[1-9][0-9]*)",
                route,
            ),
            "read_route_refused",
        )
        url = "https://api.github.com/" + route
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "hal-ci-reporting",
            },
            method="GET",
        )
        with self.opener.open(request, timeout=15) as response:
            require(response.geturl() == url, "api_destination_refused")
            raw = response.read(2_000_001)
            require(len(raw) <= 2_000_000, "api_response_budget_exceeded")
        return json.loads(raw)


def runtime_file(name: str) -> Path:
    root = Path(os.environ["RUNNER_TEMP"]).resolve()
    require(root.is_absolute() and root.is_dir(), "runtime_directory_refused")
    require(
        name in {"hal-ci-reporting.py", "hal-ci-scope.json"}, "runtime_filename_refused"
    )
    return root / name


def write_new(path: Path, raw: bytes, mode: int) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)


def outputs(value: dict[str, Any]) -> None:
    allowed = {
        "run_id",
        "run_attempt",
        "artifact_id",
        "head_sha",
        "branch",
        "pr_number",
        "coverage",
        "junit",
        "eligible",
    }
    require(set(value) <= allowed, "output_schema_refused")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, item in value.items():
            text = str(item).lower() if type(item) is bool else str(item)
            require(
                not any(ord(c) < 32 or ord(c) == 127 for c in text),
                "output_value_refused",
            )
            output.write(key + "=" + text + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["verify-event", "validate-reports"])
    args = parser.parse_args()
    try:
        require(sys.flags.isolated, "isolated_python_required")
        require(
            os.environ.get("GITHUB_EVENT_NAME") in {"workflow_run", "workflow_dispatch"}
            and os.environ.get("GITHUB_REF") == "refs/heads/main",
            "trusted_workflow_context_required",
        )
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        require(
            repository in REPOSITORIES
            and os.environ.get("GITHUB_REPOSITORY_ID") == str(REPOSITORIES[repository]),
            "runtime_repository_refused",
        )
        event_path = Path(os.environ["GITHUB_EVENT_PATH"])
        require(
            event_path.is_file() and event_path.stat().st_size <= 2_000_000,
            "event_budget_exceeded",
        )
        event = json.loads(event_path.read_bytes())
        api = ReadAPI(repository)
        if os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch":
            requested = event.get("inputs", {}).get("producer_run_id")
            require(
                isinstance(requested, str)
                and re.fullmatch(r"[1-9][0-9]{0,17}", requested),
                "requested_run_refused",
            )
            run = api(f"repos/{repository}/actions/runs/{int(requested)}")
            event = {
                "repository": {"id": REPOSITORIES[repository]},
                "workflow_run": {
                    "id": int(requested),
                    "run_attempt": run.get("run_attempt"),
                },
            }
        scope = verify_scope(event, repository, api)
        if args.operation == "verify-event":
            raw = Path(__file__).read_bytes()
            write_new(runtime_file("hal-ci-reporting.py"), raw, 0o500)
            write_new(
                runtime_file("hal-ci-scope.json"),
                json.dumps(
                    {
                        "scope": scope,
                        "controller_sha256": hashlib.sha256(raw).hexdigest(),
                    },
                    sort_keys=True,
                ).encode(),
                0o400,
            )
            outputs({k: v for k, v in scope.items() if k != "repository"})
        else:
            require(scope.get("eligible") is True, "reports_not_expected")
            sealed = json.loads(runtime_file("hal-ci-scope.json").read_bytes())
            require(
                set(sealed) == {"scope", "controller_sha256"}
                and sealed["scope"] == scope
                and hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                == sealed["controller_sha256"],
                "report_scope_changed",
            )
            result = validate_reports(Path(os.environ["REPORT_DIRECTORY"]))
            outputs({"coverage": result["coverage"], "junit": result["junit"]})
        print("CI report boundary verified; no candidate program executed.")
        return 0
    except Exception:  # noqa: BLE001 -- never echo API data or credentials
        print(
            "CI reporting refused: missing, changed or unsafe evidence.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
