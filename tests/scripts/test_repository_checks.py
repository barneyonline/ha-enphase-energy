"""Exercise the stable branch check against the actual workflow job results."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class RepositoryChecksTests(unittest.TestCase):
    def test_summary_accepts_skipped_checks_and_rejects_failed_checks(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
        jobs = workflow["jobs"]
        summary = jobs["repository-checks"]
        self.assertEqual("Repository checks", summary["name"])
        self.assertEqual("${{ always() }}", summary["if"])
        self.assertEqual(set(jobs) - {"repository-checks"}, set(summary["needs"]))
        script = summary["steps"][0]["run"]
        for result in ("success", "skipped", "failure", "cancelled"):
            with self.subTest(result=result):
                results = {name: {"result": "success"} for name in summary["needs"]}
                results[summary["needs"][-1]]["result"] = result
                completed = subprocess.run(
                    ["bash", "-e", "-c", script],
                    env={**os.environ, "RESULTS": json.dumps(results)},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    result in {"success", "skipped"}, completed.returncode == 0
                )

    def test_required_check_runs_for_every_pr_and_main_or_release_push(self) -> None:
        workflow = yaml.load(
            (ROOT / ".github/workflows/tests.yml").read_text(), Loader=yaml.BaseLoader
        )
        events = workflow["on"]
        self.assertIn("pull_request", events)
        self.assertFalse(events["pull_request"])
        self.assertEqual({"main", "release/**"}, set(events["push"]["branches"]))
        self.assertNotIn("paths", events["push"])


if __name__ == "__main__":
    unittest.main()
