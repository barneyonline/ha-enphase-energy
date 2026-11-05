"""Prepare metadata-only Codecov inputs and verify public processed delivery.

Only execute this helper from the trusted workflow revision, never the producer.
Source-tree entries and producer reports are data; no source blobs are fetched.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

PREFIX = "custom_components/enphase_ev/"
API = "https://api.codecov.io/api/v2/github/"
MAX_RESPONSE = 8 * 1024 * 1024
MAX_REPORT = 16 * 1024 * 1024
MAX_REPORTS = 32 * 1024 * 1024


def safe_path(value: str) -> str:
    """Reject aliases, special Git paths and filesystem traversal."""
    if not re.fullmatch(r"[A-Za-z0-9_./@-]{1,1024}", value) or any(
        part in {"", ".", "..", ".git"} for part in value.split("/")
    ):
        raise ValueError("Unsafe source path")
    return value


def source_paths(tree: dict, sha: str) -> set[str]:
    """Use only a complete GitHub tree tied to the verified producer commit."""
    if tree["source_sha"] != sha or tree["truncated"]:
        raise ValueError("Source tree is truncated or belongs to another commit")
    if not 0 < len(tree["tree"]) <= 10000:
        raise ValueError("Source inventory budget exceeded")
    paths = set()
    for entry in tree["tree"]:
        path = safe_path(entry["path"])
        if entry["type"] == "blob" and entry["mode"] in {"100644", "100755"}:
            if path in paths:
                raise ValueError("Duplicate source path")
            paths.add(path)
    if not any(path.startswith(PREFIX) for path in paths):
        raise ValueError("Source inventory has no integration files")
    return paths


def coverage_paths(report: Path, paths: set[str]) -> tuple[ET.ElementTree, set[str]]:
    """Normalize coverage.py paths without changing measured hits or branches."""
    raw = report.read_text(encoding="utf-8")
    if "<!doctype" in raw.lower() or "<!entity" in raw.lower():
        raise ValueError("XML declarations are not permitted")
    tree = ET.ElementTree(ET.fromstring(raw))
    root = tree.getroot()
    if root.tag != "coverage":
        raise ValueError("Expected Cobertura coverage")
    measured = set()
    seen = set()
    for item in root.findall("./packages/package/classes/class"):
        filename = safe_path(item.attrib["filename"])
        matches = {filename, PREFIX + filename} & paths
        if len(matches) != 1:
            raise ValueError("Coverage path has no unique source match")
        filename = matches.pop()
        if not filename.startswith(PREFIX) or filename in seen:
            raise ValueError("Coverage path is duplicated or outside the integration")
        seen.add(filename)
        item.set("filename", filename)
        if item.findall("./lines/line"):
            measured.add(filename)
    if not measured:
        raise ValueError("Coverage report has no measured source lines")
    sources = root.find("sources")
    if sources is None:
        sources = ET.SubElement(root, "sources")
    sources.clear()
    ET.SubElement(sources, "source").text = "."
    return tree, measured


def prepare(tree_file: Path, reports: Path, inventory: Path, sha: str) -> None:
    """Create inert filenames outside the trusted helper and report directories."""
    paths = source_paths(json.loads(tree_file.read_text()), sha)
    tree, _ = coverage_paths(reports / "coverage.xml", paths)
    normalized = ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
    total = len(normalized) + sum(
        path.stat().st_size for path in reports.iterdir() if path.name != "coverage.xml"
    )
    if len(normalized) > MAX_REPORT or total > MAX_REPORTS:
        raise ValueError("Normalized report budget exceeded")
    # A fresh directory prevents a pre-existing symlink or Git checkout being used.
    inventory.mkdir()
    for path in sorted(paths):
        target = inventory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(mode=0o444, exist_ok=False)
    (reports / "coverage.xml").write_bytes(normalized)
    print(f"Prepared {len(paths)} inert source filenames for {sha}")


def get_json(url: str) -> dict:
    """Read the public API without sending GitHub credentials or OIDC tokens."""
    if not url.startswith(API):
        raise ValueError("Unexpected Codecov API origin")
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=10) as response:
        if response.geturl() != url:
            raise ValueError("Unexpected Codecov API redirect")
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("Codecov API response budget exceeded")
    return json.loads(raw)


def uploads(base: str) -> list[dict]:
    """Follow bounded pagination using our own URLs, never server supplied URLs."""
    result = []
    for page in range(1, 11):
        data = get_json(f"{base}uploads/?format=json&page_size=100&page={page}")
        result.extend(data["results"])
        if not data["next"]:
            return result
    raise ValueError("Codecov upload pagination budget exceeded")


def nonempty(totals: dict | None) -> bool:
    return bool(totals and totals.get("files", 0) > 0 and totals.get("lines", 0) > 0)


def processed(
    commit: dict, entries: list[dict], sha: str, build: str, expected: set[str]
) -> bool:
    """Require project coverage and both exact attempt uploads, not a patch status."""
    if commit["commitid"] != sha:
        raise ValueError("Codecov returned another commit")
    for name, test_results in (("coverage", False), ("junit", True)):
        matches = [
            entry
            for entry in entries
            if entry.get("job_code" if test_results else "name")
            == f"{name}-{build.replace('.', '-')}"
        ]
        if not matches:
            return False
        for entry in matches:
            if entry["state"] == "error":
                raise ValueError(
                    f"Codecov {name} upload is unusable or processing failed"
                )
            if entry["state"] != ("processed" if test_results else "merged"):
                return False
            if test_results:
                if not entry["storage_path"].startswith("test_results/v1/raw/"):
                    raise ValueError("Expected processed JUnit test results")
            elif not nonempty(entry["totals"]):
                raise ValueError("Processed coverage upload has no source lines")
    if commit["state"] == "error":
        raise ValueError("Codecov project report is unusable or processing failed")
    if commit["state"] != "complete":
        return False
    if not nonempty(commit["totals"]) or commit["totals"].get("sessions", 0) <= 0:
        raise ValueError("Processed project report is empty")
    files = {
        item["name"]
        for item in commit["report"]["files"]
        if item["totals"].get("lines", 0) > 0
    }
    if not expected or not expected <= files:
        raise ValueError("Processed project report is missing measured source files")
    return True


def verify(repository: str, sha: str, build: str, report: Path) -> None:
    """Poll twelve times at fifteen-second intervals; fail on errors or empties."""
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
    root = ET.parse(report).getroot()
    expected = {
        item.attrib["filename"]
        for item in root.findall("./packages/package/classes/class")
        if item.findall("./lines/line")
    }
    failures = 0
    for attempt in range(12):
        try:
            commit = get_json(base + "?format=json")
            if processed(commit, uploads(base), sha, build, expected):
                print(
                    f"Codecov processed {repository}@{sha}, build {build}: coverage and JUnit"
                )
                return
        except (HTTPError, URLError, TimeoutError) as error:
            if isinstance(error, HTTPError) and error.code == 404:
                pass  # Processing may not have created the commit yet.
            else:
                failures += 1
                if failures > 2 or (
                    isinstance(error, HTTPError)
                    and error.code not in {429, 500, 502, 503, 504}
                ):
                    raise
        if attempt < 11:
            time.sleep(15)
    raise TimeoutError(
        "Codecov did not process nonempty coverage and JUnit within the polling budget"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "verify"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--tree", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--repository")
    parser.add_argument("--build")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.tree, args.reports, args.inventory, args.sha)
    else:
        verify(args.repository, args.sha, args.build, args.reports / "coverage.xml")


if __name__ == "__main__":
    main()
