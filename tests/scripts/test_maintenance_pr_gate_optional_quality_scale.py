"""The optional docs workflow remains reviewed without replacing required CI."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

import test_maintenance_pr_gate as fixtures

gate = fixtures.gate


class OptionalQualityScaleTests(unittest.TestCase):
    def setUp(self):
        self.snapshot, self.policy = fixtures.fixture()
        actual = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / ".github/maintenance/merge-policy.json"
            ).read_text()
        )
        self.optional = next(
            item
            for item in actual["checks"]
            if item["workflow_path"] == ".github/workflows/quality-scale.yml"
        )
        run = copy.deepcopy(self.snapshot["workflows"][0])
        run.update(id=21, path=".github/workflows/quality-scale.yml", run_number=1)
        run["_jobs"][0].update(
            id=101,
            name="validate",
            check_run_url="https://api.github.com/repos/example/integration/check-runs/101",
        )
        check = copy.deepcopy(self.snapshot["checks"][0])
        check.update(
            id=101,
            name="validate",
            check_suite={"id": 12},
            html_url="https://github.com/example/integration/runs/101",
        )
        check["_workflow"].update(id=21, path=run["path"], run_number=1)
        self.snapshot["workflows"].append(run)
        self.snapshot["checks"].append(check)
        self.snapshot["check_suites"].append({"id": 12})

    def result(self):
        fixtures.drop_handling_witness(self.snapshot)
        fixtures.seal(self.snapshot, self.policy)
        fixtures.attest(self.snapshot, self.policy)
        return gate.evaluate(self.snapshot, self.policy)

    def test_missing_identity_blocks_even_a_successful_docs_run(self):
        result = self.result()
        self.assertFalse(result["allow"])
        self.assertTrue(
            {"unknown_check_context", "unknown_workflow"}
            <= {item["code"] for item in result["reasons"]}
        )

    def test_actual_optional_identity_accepts_a_successful_docs_run(self):
        self.policy["checks"].append(self.optional)
        self.assertIs(self.optional["required"], False)
        result = self.result()
        self.assertTrue(result["allow"], result["reasons"])
        self.assertIn("check:101", {item["source"] for item in result["feedback"]})

    def test_optional_docs_run_never_substitutes_for_required_tests(self):
        self.policy["checks"].append(self.optional)
        self.snapshot["checks"] = [
            item for item in self.snapshot["checks"] if item["id"] != 100
        ]
        self.snapshot["check_suites"] = [
            item for item in self.snapshot["check_suites"] if item["id"] != 11
        ]
        self.snapshot["workflows"] = [
            item for item in self.snapshot["workflows"] if item["id"] != 20
        ]
        result = self.result()
        self.assertFalse(result["allow"])
        self.assertIn(
            "required_check_missing", {item["code"] for item in result["reasons"]}
        )


if __name__ == "__main__":
    unittest.main()
