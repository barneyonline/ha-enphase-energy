"""Enforce statement coverage from a report that also measures branches."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def check_report(report: dict, paths: list[str]) -> list[str]:
    """Return modules without evidence of complete statement coverage."""
    failures = []
    for path in paths:
        result = report.get("files", {}).get(path)
        if result is None:
            failures.append(f"{path}: missing coverage data")
        elif result["summary"]["covered_lines"] != result["summary"]["num_statements"]:
            failures.append(f"{path}: uncovered lines {result['missing_lines']}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--include", required=True, help="Comma-separated module paths")
    args = parser.parse_args()
    paths = [path for path in args.include.split(",") if path]
    if not paths:
        parser.error("--include must name at least one module")
    failures = check_report(json.loads(args.report.read_text()), paths)
    for failure in failures:
        print(failure)
    if not failures:
        print(f"100% statement coverage across {len(paths)} changed modules")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
