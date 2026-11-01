"""Preserve event-specific validation contracts and supported manual recovery."""

from __future__ import annotations

import copy
import unittest

import test_maintenance_pr_gate as fixtures

gate = fixtures.gate


class EventContractTests(unittest.TestCase):
    def setUp(self):
        self.snapshot, self.policy = fixtures.fixture()
        self.assertTrue(gate.evaluate(self.snapshot, self.policy)["allow"])

    def manual_success(self, old_conclusion="failure"):
        old_run = self.snapshot["workflows"][0]
        old_check = self.snapshot["checks"][0]
        old_run["conclusion"] = old_conclusion
        old_check["conclusion"] = old_conclusion
        old_check["output"]["summary"] = "Earlier PR contract did not pass"

        manual = copy.deepcopy(old_run)
        manual.update(
            id=21,
            event="workflow_dispatch",
            run_number=6,
            conclusion="success",
            pull_requests=[],
        )
        manual["_jobs"][0].update(
            id=102,
            check_run_url=(
                "https://api.github.com/repos/example/integration/check-runs/102"
            ),
        )
        current = copy.deepcopy(old_check)
        current.update(
            id=102,
            conclusion="success",
            check_suite={"id": 12},
            html_url="https://github.com/example/integration/runs/102",
        )
        current["output"]["summary"] = "Later manual validation passed"
        current["_workflow"].update(id=21, run_number=6)
        self.snapshot["workflows"].append(manual)
        self.snapshot["checks"].append(current)
        self.snapshot["check_suites"].append({"id": 12})
        return old_run, manual, old_check, current

    def evaluate(self, expected_checks=(100, 102)):
        fixtures.drop_handling_witness(self.snapshot)
        fixtures.seal(self.snapshot, self.policy)
        fixtures.attest(self.snapshot, self.policy)
        feedback, ledger, latest = gate.inventory(self.snapshot, self.policy)
        sources = {item["source"] for item in feedback}
        entries = {entry["source"] for entry in ledger["entries"]}
        for check in expected_checks:
            self.assertIn(f"check:{check}", sources)
            self.assertIn(f"check:{check}", entries)
        self.assertEqual([check["id"] for check in latest], [102])
        result = gate.evaluate(self.snapshot, self.policy)
        self.assertIn("handling_evidence", result)
        self.assertNotIn("invalid_evidence", {r["code"] for r in result["reasons"]})
        return result

    def test_manual_success_cannot_mask_failed_pr_contract_with_same_names(self):
        old, manual, old_check, current = self.manual_success()
        self.assertEqual(old["path"], manual["path"])
        self.assertEqual(old["head_sha"], manual["head_sha"])
        self.assertEqual(old_check["name"], current["name"])
        self.assertEqual(old["_jobs"][0]["name"], manual["_jobs"][0]["name"])
        result = self.evaluate()
        self.assertFalse(result["allow"])
        self.assertEqual(
            result["reasons"],
            [
                {
                    "code": "latest_workflow_not_successful",
                    "run": 20,
                    "path": ".github/workflows/tests.yml",
                }
            ],
        )

    def test_manual_success_cannot_mask_cancelled_pr_contract(self):
        self.manual_success("cancelled")
        result = self.evaluate()
        self.assertFalse(result["allow"])
        self.assertEqual(
            result["reasons"],
            [
                {
                    "code": "latest_workflow_not_successful",
                    "run": 20,
                    "path": ".github/workflows/tests.yml",
                }
            ],
        )

    def test_absent_path_filtered_pr_run_allows_successful_manual_validation(self):
        _, manual, _, current = self.manual_success()
        self.snapshot["workflows"] = [manual]
        self.snapshot["checks"] = [current]
        self.snapshot["check_suites"] = [{"id": 12}]
        result = self.evaluate(expected_checks=(102,))
        self.assertTrue(result["allow"], result["reasons"])

    def test_same_event_successful_rerun_supersedes_failure_with_audit_retained(self):
        old, rerun, _, current = self.manual_success()
        rerun.update(
            id=20,
            event="pull_request",
            run_number=5,
            run_attempt=2,
            pull_requests=copy.deepcopy(old["pull_requests"]),
        )
        rerun["_jobs"][0]["run_attempt"] = 2
        current["_workflow"].update(id=20, run_number=5, run_attempt=2)
        self.snapshot["workflows"] = [rerun]
        result = self.evaluate()
        self.assertTrue(result["allow"], result["reasons"])

    def test_manual_run_does_not_hide_older_pr_base_mismatch(self):
        old, _, _, _ = self.manual_success()
        old["pull_requests"][0]["base"]["sha"] = "c" * 40
        result = self.evaluate()
        self.assertFalse(result["allow"])
        self.assertEqual(
            {reason["code"] for reason in result["reasons"]},
            {"workflow_current_base_unproven", "latest_workflow_not_successful"},
        )

    def test_same_context_manual_success_supersedes_intentional_check_skip(self):
        old, _, old_check, _ = self.manual_success()
        old["conclusion"] = "success"
        old_check["conclusion"] = "skipped"
        old_check["output"]["summary"] = "Intentionally path-classified skip"
        result = self.evaluate()
        self.assertTrue(result["allow"], result["reasons"])


if __name__ == "__main__":
    unittest.main()
