"""Statement gates must stay independent of diagnostic branch coverage."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

# Script tests also run without the integration conftest or repository imports.
check_report = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/check_statement_coverage.py")
)["check_report"]


@pytest.mark.parametrize(
    "covered,expected", [(2, []), (1, ["module.py: uncovered lines [3]"])]
)
def test_statement_gate_ignores_missing_branches(covered, expected):
    report = {
        "files": {
            "module.py": {
                "summary": {
                    "covered_lines": covered,
                    "num_statements": 2,
                    "missing_branches": 1,
                },
                "missing_lines": [3] if covered < 2 else [],
            }
        }
    }
    assert check_report(report, ["module.py"]) == expected


def test_missing_module_fails_closed():
    assert check_report({"files": {}}, ["new.py"]) == ["new.py: missing coverage data"]


@pytest.mark.parametrize(
    "include,status", [("empty.py", 0), ("missing.py", 1), ("", 2)]
)
def test_command_line(tmp_path, include, status):
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "files": {
                    "empty.py": {
                        "summary": {"covered_lines": 0, "num_statements": 0},
                        "missing_lines": [],
                    }
                }
            }
        )
    )
    script = Path(__file__).resolve().parents[2] / "scripts/check_statement_coverage.py"
    result = subprocess.run(
        [sys.executable, str(script), "--report", str(report), "--include", include],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == status
