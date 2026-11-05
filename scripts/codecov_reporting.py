"""Confirm Codecov processed the producer commit's coverage and test results."""

from __future__ import annotations

import argparse
import json
import re
import time
from urllib.error import HTTPError
from urllib.request import urlopen

API = "https://api.codecov.io/api/v2/github/"


def get_json(url: str) -> dict:
    """Use Codecov's public API without reporting or GitHub credentials."""
    with urlopen(url, timeout=10) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Codecov API response budget exceeded")
    return json.loads(raw)


def uploads(base: str) -> list[dict]:
    entries = []
    for page in range(1, 11):
        data = get_json(f"{base}uploads/?format=json&page_size=100&page={page}")
        entries.extend(data["results"])
        if not data["next"]:
            return entries
    raise ValueError("Codecov upload pagination budget exceeded")


def nonempty(totals: dict | None) -> bool:
    return bool(totals and totals.get("files", 0) > 0 and totals.get("lines", 0) > 0)


def processed(commit: dict, entries: list[dict], sha: str, build: str) -> bool:
    """Check the project report and exact kind-specific run/attempt uploads."""
    if commit["commitid"] != sha:
        raise ValueError("Codecov returned another commit")
    for kind, key, state in (
        ("coverage", "name", "merged"),
        ("junit", "job_code", "processed"),
    ):
        matches = [
            entry
            for entry in entries
            if entry.get(key) == f"{kind}-{build.replace('.', '-')}"
        ]
        if not matches:
            return False
        for entry in matches:
            if entry["state"] == "error":
                raise ValueError(
                    f"Codecov {kind} report is unusable or processing failed"
                )
            if entry["state"] != state:
                return False
            if kind == "coverage" and not nonempty(entry["totals"]):
                raise ValueError("Codecov coverage upload has no measured source lines")
    if commit["state"] == "error":
        raise ValueError("Codecov project report is unusable or processing failed")
    if commit["state"] != "complete":
        return False
    if not nonempty(commit["totals"]) or not commit["report"]["files"]:
        raise ValueError("Codecov processed an empty project report")
    return True


def verify(repository: str, sha: str, build: str) -> None:
    """Poll boundedly for nonempty coverage and processed test results."""
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository
    ):
        raise ValueError("Invalid repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(
        r"[1-9][0-9]*\.[1-9][0-9]*", build
    ):
        raise ValueError("Invalid producer attribution")
    owner, repo = repository.split("/")
    base = f"{API}{owner}/repos/{repo}/commits/{sha}/"
    for attempt in range(12):
        try:
            if processed(get_json(base + "?format=json"), uploads(base), sha, build):
                print(
                    f"Codecov processed {repository}@{sha}, build {build}: coverage and JUnit"
                )
                return
        except HTTPError as error:
            if error.code != 404:
                raise
        if attempt < 11:
            time.sleep(15)
    raise TimeoutError(
        "Codecov did not process coverage and JUnit within the polling budget"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--build", required=True)
    args = parser.parse_args()
    verify(args.repository, args.sha, args.build)


if __name__ == "__main__":
    main()
