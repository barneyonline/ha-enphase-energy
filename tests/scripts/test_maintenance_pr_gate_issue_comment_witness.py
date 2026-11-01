"""Handling witnesses must avoid recursively creating review observer checks."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

try:
    import test_maintenance_pr_gate as fixtures
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).parent))
    import test_maintenance_pr_gate as fixtures

gate = fixtures.gate


class IssueCommentWitnessTests(unittest.TestCase):
    def remove_issue_witness(self, snapshot):
        snapshot["issue_comments"] = [
            comment
            for comment in snapshot["issue_comments"]
            if gate.CLEAN_MARKER not in comment["body"]
        ]

    def bound_marker(self, snapshot, policy, excluded):
        ledger = fixtures.seal(snapshot, policy)
        feedback, _, _ = gate.inventory(snapshot, policy)
        targets = [item for item in feedback if item["source"] != excluded]
        entries = {entry["source"]: entry for entry in ledger["entries"]}
        verdict = {
            "schema": 1,
            "repository": fixtures.REPOSITORY,
            "pull_number": 1,
            "head_sha": fixtures.HEAD,
            "base_sha": fixtures.BASE,
            "policy_sha256": gate.digest(policy),
            "feedback_digest": gate.inventory_digest(targets),
            "dispositions_digest": gate.digest(
                [entries[item["source"]] for item in targets]
            ),
            "verdict": "clean",
            "reason": "Independently reviewed the current feedback and handling.",
            "evidence_urls": [fixtures.URL],
        }
        return "<!-- " + gate.CLEAN_MARKER + " " + json.dumps(verdict) + " -->"

    def add_observer(self, snapshot, policy):
        path = ".github/workflows/maintenance-review-events.yml"
        policy["checks"].append(
            {
                "app_id": 15368,
                "workflow_path": path,
                "name": "review-event",
                "required": False,
            }
        )
        check = copy.deepcopy(snapshot["checks"][0])
        check.update(id=200, name="review-event", check_suite={"id": 12})
        check["_workflow"].update(id=21, path=path)
        run = copy.deepcopy(snapshot["workflows"][0])
        run.update(
            id=21,
            path=path,
            event="pull_request_review",
            _jobs=[
                {
                    "id": 200,
                    "name": "review-event",
                    "run_attempt": 1,
                    "check_run_url": "https://api.github.com/repos/example/integration/check-runs/200",
                }
            ],
        )
        snapshot["checks"].append(check)
        snapshot["check_suites"].append({"id": 12})
        snapshot["workflows"].append(run)
        return check, run

    def test_exact_formal_review_marker_cannot_supply_handling_authority(self):
        for state in ("COMMENTED", "APPROVED"):
            with self.subTest(state=state):
                snapshot, policy = fixtures.fixture()
                self.remove_issue_witness(snapshot)
                review = snapshot["reviews"][1]
                meta = snapshot["review_meta"][1]
                review["state"] = meta["state"] = state
                review["body"] = meta["body"] = self.bound_marker(
                    snapshot, policy, "review:31"
                )
                fixtures.seal(snapshot, policy)
                result = gate.evaluate(snapshot, policy)
                self.assertFalse(result["allow"], result)
                self.assertIn(
                    "independent_handling_verdict_missing_or_stale",
                    fixtures.codes(snapshot, policy),
                )
                self.assertIn("review:31", {f["source"] for f in result["feedback"]})

    def test_exact_inline_review_marker_cannot_supply_handling_authority(self):
        snapshot, policy = fixtures.fixture()
        self.remove_issue_witness(snapshot)
        snapshot["reviews"][1]["body"] = "Ordinary independent formal review."
        snapshot["review_meta"][1]["body"] = snapshot["reviews"][1]["body"]
        inline = {
            "id": 40,
            "user": fixtures.INDEPENDENT,
            "body": "Preparing independent handling marker.",
            "updated_at": fixtures.LATER,
            "html_url": "https://github.com/example/integration/pull/1#discussion_r40",
        }
        snapshot["review_comments"].append(inline)
        thread_comment = {
            "databaseId": 40,
            "body": inline["body"],
            "updatedAt": inline["updated_at"],
        }
        snapshot["threads"].append(
            {
                "id": "thread40",
                "isResolved": True,
                "isOutdated": False,
                "comments": [thread_comment],
            }
        )
        inline["body"] = thread_comment["body"] = self.bound_marker(
            snapshot, policy, "inline_comment:40"
        )
        fixtures.seal(snapshot, policy)
        result = gate.evaluate(snapshot, policy)
        self.assertFalse(result["allow"], result)
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            fixtures.codes(snapshot, policy),
        )

    def test_issue_comment_after_settled_observer_remains_valid_and_covers_it(self):
        snapshot, policy = fixtures.fixture()
        self.add_observer(snapshot, policy)
        fixtures.seal(snapshot, policy)
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            fixtures.codes(snapshot, policy),
        )
        fixtures.attest(snapshot, policy)
        collected, actual = gate.GitHub(
            fixtures.REPOSITORY,
            "fixture-placeholder",
            fixtures.ClientTests().full_transport(snapshot, policy),
        ).collect(1)
        result = gate.evaluate(collected, actual)
        self.assertTrue(result["allow"], result)
        self.assertTrue(
            result["handling_evidence"]["source"].startswith("issue_comment:")
        )
        self.assertIn("check:200", {f["source"] for f in result["feedback"]})
        self.assertEqual(result, gate.evaluate(copy.deepcopy(collected), actual))

    def test_failed_or_pending_observer_is_never_excluded(self):
        for status, conclusion in (("completed", "failure"), ("in_progress", None)):
            with self.subTest(status=status):
                snapshot, policy = fixtures.fixture()
                check, run = self.add_observer(snapshot, policy)
                check.update(status=status, conclusion=conclusion)
                run.update(status=status, conclusion=conclusion)
                fixtures.seal(snapshot, policy)
                fixtures.attest(snapshot, policy)
                result = gate.evaluate(snapshot, policy)
                self.assertFalse(result["allow"], result)
                self.assertIn(
                    "latest_check_not_successful", fixtures.codes(snapshot, policy)
                )
                self.assertIn("check:200", {f["source"] for f in result["feedback"]})


if __name__ == "__main__":
    unittest.main()
