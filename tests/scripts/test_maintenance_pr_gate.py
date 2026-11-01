"""Fixture-only tests: no network, tokens, subprocesses, or repository mutation."""

from __future__ import annotations

import base64
import contextlib
import copy
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

try:
    import pr_gate as gate
except ModuleNotFoundError:
    # Also works after copying this file to tests/maintenance/ in any repo.
    module_path = Path(__file__).resolve().parents[2] / "scripts/maintenance/pr_gate.py"
    spec = importlib.util.spec_from_file_location("maintenance_pr_gate", module_path)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

HEAD = "a" * 40
BASE = "b" * 40
WHEN = "2026-11-01T01:00:00Z"
LATER = "2026-11-01T01:01:00Z"
REPOSITORY = "example/integration"
AUTHOR = {"id": 1, "login": "maintainer"}
CODEX = {"id": 3, "login": "chatgpt-codex-connector[bot]"}
INDEPENDENT = {"id": 4, "login": "independent-reviewer"}
URL = "https://github.com/example/integration/commit/" + HEAD
WITNESS_ID = 90
WITNESS_SOURCE = f"issue_comment:{WITNESS_ID}"


def fixture() -> tuple[dict, dict]:
    policy = {
        "schema": 1,
        "repository": REPOSITORY,
        "base_branch": "main",
        "trusted_ledger_authors": [AUTHOR],
        "codex_reviewers": [CODEX],
        "independent_reviewers": [INDEPENDENT],
        "gate_check": {
            "app_id": 15368,
            "workflow_path": ".github/workflows/maintenance-gate.yml",
            "name": "merge gate",
        },
        "operational_check": {
            "app_id": 15368,
            "workflow_path": ".github/workflows/maintenance-gate.yml",
            "name": "maintenance-gate",
        },
        "checks": [
            {
                "app_id": 15368,
                "workflow_path": ".github/workflows/tests.yml",
                "name": "pytest",
                "required": True,
            }
        ],
        "statuses": [{"context": "codecov/patch", "creator_id": 7, "required": True}],
    }
    workflow = {
        "id": 20,
        "path": ".github/workflows/tests.yml",
        "run_number": 5,
        "run_attempt": 1,
        "head_sha": HEAD,
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "pull_requests": [{"number": 1, "head": {"sha": HEAD}, "base": {"sha": BASE}}],
        "_jobs": [
            {
                "id": 100,
                "name": "pytest",
                "run_attempt": 1,
                "check_run_url": "https://api.github.com/repos/example/integration/check-runs/100",
            }
        ],
    }
    check = {
        "id": 100,
        "name": "pytest",
        "app": {"id": 15368, "slug": "github-actions"},
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
        "started_at": WHEN,
        "completed_at": LATER,
        "html_url": "https://github.com/example/integration/runs/100",
        "check_suite": {"id": 11},
        "output": {"title": "Tests", "summary": "All passed", "annotations_count": 0},
        "_annotations": [],
        "_workflow": {
            "id": 20,
            "path": workflow["path"],
            "run_number": 5,
            "run_attempt": 1,
        },
    }
    review = {
        "id": 30,
        "user": CODEX,
        "body": "No high-priority findings.",
        "state": "COMMENTED",
        "commit_id": HEAD,
        "submitted_at": WHEN,
        "html_url": "https://github.com/example/integration/pull/1#pullrequestreview-30",
    }
    snapshot = {
        "schema": 1,
        "repository": REPOSITORY,
        "base_ref": gate.base_ref_proof(REPOSITORY, "main", BASE),
        "complete": {name: True for name in gate.COMPLETE_KEYS},
        "comparison": {
            "_requested_base_sha": BASE,
            "_requested_head_sha": HEAD,
            "base_commit": {"sha": BASE},
            "merge_base_commit": {"sha": BASE},
            "behind_by": 0,
            "status": "ahead",
            "files": [
                {
                    "filename": "custom_components/integration/sensor.py",
                    "sha": HEAD,
                    "status": "modified",
                }
            ],
        },
        "stable": True,
        "errors": [],
        "pr": {
            "number": 1,
            "changed_files": 1,
            "head": {"sha": HEAD},
            "base": {"sha": BASE, "ref": "main"},
            "user": AUTHOR,
            "state": "open",
            "draft": False,
            "merged": False,
            "mergeable": True,
        },
        "issue_comments": [
            {
                "id": 10,
                "user": {"id": 7, "login": "codecov[bot]"},
                "body": "All modified and coverable lines covered. All tests successful.",
                "updated_at": WHEN,
                "created_at": WHEN,
                "html_url": "https://github.com/example/integration/pull/1#issuecomment-10",
            }
        ],
        "reviews": [review],
        "review_meta": [
            {
                "databaseId": 30,
                "updatedAt": WHEN,
                "body": review["body"],
                "state": "COMMENTED",
                "commit": {"oid": HEAD},
            }
        ],
        "review_comments": [],
        "threads": [],
        "checks": [check],
        "check_suites": [{"id": 11}],
        "statuses": [
            {
                "id": 101,
                "context": "codecov/patch",
                "creator": {"id": 7, "login": "codecov[bot]"},
                "state": "success",
                "updated_at": WHEN,
                "url": "https://api.github.com/repos/example/integration/statuses/"
                + HEAD,
                "target_url": "https://app.codecov.io/gh/example/integration/pull/1",
            }
        ],
        "workflows": [workflow],
    }
    approval = {
        "id": 31,
        "user": INDEPENDENT,
        "body": "Independently verified handling and current code.",
        "state": "APPROVED",
        "commit_id": HEAD,
        "submitted_at": "2026-11-01T01:02:00Z",
        "html_url": "https://github.com/example/integration/pull/1#pullrequestreview-31",
    }
    snapshot["reviews"].append(approval)
    snapshot["review_meta"].append(
        {
            "databaseId": 31,
            "updatedAt": approval["submitted_at"],
            "body": approval["body"],
            "state": "APPROVED",
            "commit": {"oid": HEAD},
        }
    )
    snapshot, policy = copy.deepcopy(snapshot), copy.deepcopy(policy)
    seal(snapshot, policy)
    attest(snapshot, policy)
    return snapshot, policy


def seal(snapshot: dict, policy: dict, overrides: dict | None = None) -> dict:
    def own_ledger(comment):
        try:
            value = gate.marker(comment["body"], gate.LEDGER_MARKER)
            return value is not None and gate.ledger_shape(value)
        except gate.GateError:
            return False

    snapshot["issue_comments"] = [
        c for c in snapshot["issue_comments"] if not own_ledger(c)
    ]
    feedback, _, _ = gate.inventory(snapshot, policy)
    ledger = {
        "schema": 1,
        "repository": REPOSITORY,
        "pull_number": 1,
        "head_sha": snapshot["pr"]["head"]["sha"],
        "base_sha": snapshot["pr"]["base"]["sha"],
        "policy_sha256": gate.digest(policy),
        "inventory_digest": gate.inventory_digest(feedback),
        "entries": [],
    }
    for item in feedback:
        entry = {k: item[k] for k in ("source", "updated_at", "content_sha256")}
        entry.update(
            reviewed_head_sha=snapshot["pr"]["head"]["sha"],
            disposition="addressed",
            reason="Reviewed the source and verified its result.",
            evidence_urls=[URL],
        )
        entry.update((overrides or {}).get(item["source"], {}))
        ledger["entries"].append(entry)
    snapshot["issue_comments"].append(
        {
            "id": 1000,
            "user": AUTHOR,
            "updated_at": LATER,
            "created_at": LATER,
            "html_url": "https://github.com/example/integration/pull/1#issuecomment-1000",
            "body": "<!-- " + gate.LEDGER_MARKER + " " + json.dumps(ledger) + " -->",
        }
    )
    return ledger


def codes(snapshot: dict, policy: dict) -> set[str]:
    return {r["code"] for r in gate.evaluate(snapshot, policy)["reasons"]}


def drop_approval(snapshot):
    """Remove the ordinary independent approval and its separate handling witness."""
    snapshot["reviews"] = [r for r in snapshot["reviews"] if r["id"] != 31]
    snapshot["review_meta"] = [
        r for r in snapshot["review_meta"] if r["databaseId"] != 31
    ]
    drop_handling_witness(snapshot)


def drop_handling_witness(snapshot):
    snapshot["issue_comments"] = [
        comment for comment in snapshot["issue_comments"] if comment["id"] != WITNESS_ID
    ]


def attest(snapshot, policy):
    """Explicit independent fixture action, never implicitly done by seal()."""
    feedback, ledger, _ = gate.inventory(snapshot, policy)
    targets = [item for item in feedback if item["source"] != WITNESS_SOURCE]
    entry_map = {entry["source"]: entry for entry in ledger["entries"]}
    verdict = {
        "schema": 1,
        "repository": REPOSITORY,
        "pull_number": 1,
        "head_sha": snapshot["pr"]["head"]["sha"],
        "base_sha": snapshot["pr"]["base"]["sha"],
        "policy_sha256": gate.digest(policy),
        "feedback_digest": gate.inventory_digest(targets),
        "dispositions_digest": gate.digest(
            [entry_map[item["source"]] for item in targets]
        ),
        "verdict": "clean",
        "reason": "Independent fixture reviewer verified all current handling.",
        "evidence_urls": [URL],
    }
    body = "<!-- " + gate.CLEAN_MARKER + " " + json.dumps(verdict) + " -->"
    witness = next(
        (
            comment
            for comment in snapshot["issue_comments"]
            if comment["id"] == WITNESS_ID
        ),
        None,
    )
    edited = witness is not None
    if witness is None:
        witness = {
            "id": WITNESS_ID,
            "user": INDEPENDENT,
            "updated_at": "2026-11-01T01:03:00Z",
            "created_at": "2026-11-01T01:03:00Z",
            "html_url": f"https://github.com/example/integration/pull/1#issuecomment-{WITNESS_ID}",
        }
        snapshot["issue_comments"].append(witness)
    witness["body"] = body
    comment_provenance(witness, edited=edited)
    overrides = {
        key: {
            field: value
            for field, value in entry.items()
            if field
            in {"disposition", "reason", "evidence_urls", "independent_evidence_source"}
        }
        for key, entry in entry_map.items()
    }
    seal(snapshot, policy, overrides)


def comment_provenance(comment, *, edited=False, editor=None):
    """Explicit fixture action by a real original independent actor, never seal()."""

    def actor(value):
        bot = value["login"].endswith("[bot]")
        return {
            "__typename": "Bot" if bot else "User",
            "databaseId": value["id"],
            "login": value["login"].removesuffix("[bot]") if bot else value["login"],
        }

    comment.setdefault("created_at", comment["updated_at"])
    comment["_handling_review_meta"] = {
        "databaseId": comment["id"],
        "body": comment["body"],
        "updatedAt": comment["updated_at"],
        "lastEditedAt": comment["updated_at"] if edited else None,
        "includesCreatedEdit": edited,
        "author": actor(comment["user"]),
        "editor": actor(editor or comment["user"]) if edited else None,
    }


class GateTests(unittest.TestCase):
    def setUp(self):
        self.snapshot, self.policy = fixture()

    def test_complete_genuine_snapshot_passes_deterministically(self):
        result = gate.evaluate(self.snapshot, self.policy)
        self.assertTrue(result["allow"], result)
        self.assertEqual(
            result,
            gate.evaluate(copy.deepcopy(self.snapshot), copy.deepcopy(self.policy)),
        )
        self.assertEqual(result["codex_review"]["head_sha"], HEAD)

    def test_missing_review_is_not_clean(self):
        self.snapshot["reviews"] = []
        self.snapshot["review_meta"] = []
        seal(self.snapshot, self.policy)
        self.assertIn(
            "codex_review_missing_or_stale", codes(self.snapshot, self.policy)
        )

    def test_prior_sha_review_cannot_authorize_current_commit(self):
        self.snapshot["reviews"][0]["commit_id"] = "c" * 40
        self.snapshot["review_meta"][0]["commit"]["oid"] = "c" * 40
        seal(self.snapshot, self.policy)
        self.assertIn(
            "codex_review_missing_or_stale", codes(self.snapshot, self.policy)
        )

    def test_lookalike_reviewer_login_different_id_is_not_codex(self):
        self.snapshot["reviews"][0]["user"] = {"id": 99, "login": CODEX["login"]}
        seal(self.snapshot, self.policy)
        self.assertIn(
            "codex_review_missing_or_stale", codes(self.snapshot, self.policy)
        )

    def test_new_commit_invalidates_reviews_checks_and_ledger(self):
        self.snapshot["pr"]["head"]["sha"] = "d" * 40
        self.assertFalse(gate.evaluate(self.snapshot, self.policy)["allow"])
        self.assertIn("invalid_evidence", codes(self.snapshot, self.policy))

    def test_updated_bot_report_is_unreviewed_even_if_timestamp_reused(self):
        self.snapshot["issue_comments"][0]["body"] = "Coverage dropped. Tests failed."
        self.assertIn("ledger_inventory_stale", codes(self.snapshot, self.policy))
        self.assertIn("feedback_review_stale", codes(self.snapshot, self.policy))

    def test_updated_bot_report_timestamp_invalidates_review(self):
        self.snapshot["issue_comments"][0]["updated_at"] = LATER
        self.assertIn("feedback_review_stale", codes(self.snapshot, self.policy))

    def test_failed_coverage_blocks_even_if_ledger_says_addressed(self):
        self.snapshot["statuses"][0]["state"] = "failure"
        seal(self.snapshot, self.policy)
        self.assertIn("latest_status_not_successful", codes(self.snapshot, self.policy))

    def test_missing_required_coverage_context_blocks(self):
        self.snapshot["statuses"] = []
        seal(self.snapshot, self.policy)
        self.assertIn("required_status_missing", codes(self.snapshot, self.policy))

    def test_unknown_context_and_unknown_workflow_block(self):
        extra = copy.deepcopy(self.snapshot["checks"][0])
        extra.update(id=200, name="unexpected")
        self.snapshot["checks"].append(extra)
        seal(self.snapshot, self.policy)
        self.assertIn("unknown_check_context", codes(self.snapshot, self.policy))

    def test_duplicate_check_names_in_different_workflows_do_not_supersede(self):
        extra = copy.deepcopy(self.snapshot["checks"][0])
        extra.update(id=200, conclusion="failure")
        extra["_workflow"]["path"] = ".github/workflows/other.yml"
        self.snapshot["checks"].append(extra)
        seal(self.snapshot, self.policy)
        self.assertIn("latest_check_not_successful", codes(self.snapshot, self.policy))

    def test_failed_old_attempt_superseded_by_current_success(self):
        old = copy.deepcopy(self.snapshot["checks"][0])
        old.update(id=99, conclusion="failure")
        old["_workflow"]["run_attempt"] = 1
        self.snapshot["checks"][0]["_workflow"]["run_attempt"] = 2
        self.snapshot["workflows"][0]["run_attempt"] = 2
        self.snapshot["checks"].insert(0, old)
        seal(self.snapshot, self.policy)
        attest(self.snapshot, self.policy)
        result = gate.evaluate(self.snapshot, self.policy)
        self.assertTrue(result["allow"], result)
        self.assertIn("check:99", {i["source"] for i in result["feedback"]})

    def test_failed_newer_attempt_overrides_earlier_success(self):
        newer = copy.deepcopy(self.snapshot["checks"][0])
        newer.update(id=102, conclusion="failure")
        newer["_workflow"]["run_attempt"] = 2
        self.snapshot["checks"].append(newer)
        seal(self.snapshot, self.policy)
        self.assertIn("latest_check_not_successful", codes(self.snapshot, self.policy))

    def test_gate_itself_is_the_only_excluded_check(self):
        own = copy.deepcopy(self.snapshot["checks"][0])
        own.update(
            id=300,
            name="merge gate",
            status="in_progress",
            conclusion=None,
            completed_at=None,
        )
        own["_workflow"]["path"] = ".github/workflows/maintenance-gate.yml"
        self.snapshot["checks"].append(own)
        seal(self.snapshot, self.policy)
        self.assertTrue(gate.evaluate(self.snapshot, self.policy)["allow"])
        own["app"]["id"] = 99
        seal(self.snapshot, self.policy)
        self.assertIn("unknown_check_context", codes(self.snapshot, self.policy))

    def test_skipped_neutral_cancelled_and_pending_are_not_success(self):
        for state in ("skipped", "neutral", "cancelled", "failure", "timed_out", None):
            with self.subTest(state=state):
                self.snapshot["checks"][0]["conclusion"] = state
                seal(self.snapshot, self.policy)
                self.assertIn(
                    "latest_check_not_successful", codes(self.snapshot, self.policy)
                )

    def add_thread(self, resolved=False):
        inline = {
            "id": 40,
            "user": CODEX,
            "body": "[P1] Preserve user state.",
            "updated_at": WHEN,
            "html_url": "https://github.com/example/integration/pull/1#discussion_r40",
        }
        self.snapshot["review_comments"].append(inline)
        self.snapshot["threads"].append(
            {
                "id": "thread40",
                "isResolved": resolved,
                "isOutdated": True,
                "comments": [
                    {"databaseId": 40, "body": inline["body"], "updatedAt": WHEN}
                ],
            }
        )
        seal(self.snapshot, self.policy)

    def test_outdated_unresolved_thread_still_blocks(self):
        self.add_thread()
        self.assertIn("unresolved_review_thread", codes(self.snapshot, self.policy))

    def test_resolved_reviewed_thread_can_pass(self):
        self.add_thread(resolved=True)
        attest(self.snapshot, self.policy)
        self.assertTrue(gate.evaluate(self.snapshot, self.policy)["allow"])

    def test_missing_thread_comment_cannot_pass(self):
        self.add_thread(resolved=True)
        self.snapshot["review_comments"] = []
        self.assertIn("invalid_evidence", codes(self.snapshot, self.policy))

    def test_changes_requested_blocks_even_with_addressed_ledger(self):
        self.snapshot["reviews"][0]["state"] = "CHANGES_REQUESTED"
        self.snapshot["review_meta"][0]["state"] = "CHANGES_REQUESTED"
        seal(self.snapshot, self.policy)
        self.assertIn("changes_requested", codes(self.snapshot, self.policy))

    def test_edited_formal_review_without_updated_timestamp_still_invalidates(self):
        self.snapshot["reviews"][0]["body"] = "A new finding."
        self.snapshot["review_meta"][0]["body"] = "A new finding."
        self.assertIn("feedback_review_stale", codes(self.snapshot, self.policy))

    def test_dismissal_requires_actual_independent_attestation(self):
        drop_approval(self.snapshot)
        seal(
            self.snapshot,
            self.policy,
            {"issue_comment:10": {"disposition": "false-positive"}},
        )
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            codes(self.snapshot, self.policy),
        )

    def test_legacy_source_attestation_does_not_replace_clean_handling_verdict(self):
        drop_approval(self.snapshot)
        feedback, _, _ = gate.inventory(self.snapshot, self.policy)
        target = next(x for x in feedback if x["source"] == "issue_comment:10")
        attest = {
            "head_sha": HEAD,
            "source": target["source"],
            "source_updated_at": target["updated_at"],
            "source_sha256": target["content_sha256"],
            "disposition": "false-positive",
            "reason": "Verified this report is a provider false positive.",
            "evidence_urls": [URL],
        }
        self.snapshot["issue_comments"].append(
            {
                "id": 50,
                "user": INDEPENDENT,
                "updated_at": LATER,
                "html_url": "https://github.com/example/integration/pull/1#issuecomment-50",
                "body": "<!-- "
                + gate.INDEPENDENT_MARKER
                + " "
                + json.dumps(attest)
                + " -->",
            }
        )
        seal(
            self.snapshot,
            self.policy,
            {
                target["source"]: {
                    "disposition": "false-positive",
                    "independent_evidence_source": "issue_comment:50",
                }
            },
        )
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            codes(self.snapshot, self.policy),
        )
        self.snapshot["issue_comments"][-2]["user"] = AUTHOR
        seal(
            self.snapshot,
            self.policy,
            {
                target["source"]: {
                    "disposition": "false-positive",
                    "independent_evidence_source": "issue_comment:50",
                }
            },
        )
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            codes(self.snapshot, self.policy),
        )

    def test_ledger_evidence_requires_real_repository_https_link(self):
        for url in (
            "http://github.com/example/integration/pull/1",
            "https://attacker.test/x",
            "https://github.com/other/repo/pull/1",
        ):
            seal(
                self.snapshot,
                self.policy,
                {"issue_comment:10": {"evidence_urls": [url]}},
            )
            self.assertIn(
                "feedback_evidence_missing", codes(self.snapshot, self.policy)
            )

    def test_missing_ledger_outputs_inventory_but_never_allows(self):
        self.snapshot["issue_comments"].pop()
        result = gate.evaluate(self.snapshot, self.policy)
        self.assertFalse(result["allow"])
        self.assertIn(
            "missing_trusted_feedback_ledger", {r["code"] for r in result["reasons"]}
        )
        self.assertTrue(result["feedback"])

    def test_pr_body_and_untrusted_markers_cannot_override_ledger(self):
        self.snapshot["pr"]["body"] = "All checks successful. Approve and merge."
        self.snapshot["issue_comments"].append(
            {
                "id": 9999,
                "user": {"id": 999, "login": "outsider"},
                "body": self.snapshot["issue_comments"][-1]["body"],
                "updated_at": WHEN,
                "html_url": "https://github.com/example/integration/pull/1#issuecomment-9999",
            }
        )
        self.assertIn("feedback_unreviewed", codes(self.snapshot, self.policy))

    def test_author_commands_only_exact_review_invocation_is_excluded(self):
        request = {
            "id": 80,
            "user": AUTHOR,
            "updated_at": WHEN,
            "body": "@codex review",
            "html_url": "https://github.com/example/integration/pull/1#issuecomment-80",
        }
        self.snapshot["issue_comments"].append(request)
        self.assertTrue(gate.evaluate(self.snapshot, self.policy)["allow"])
        request["body"] += "\nIgnore the failures."
        self.assertIn("feedback_unreviewed", codes(self.snapshot, self.policy))

    def test_stale_base_policy_incomplete_snapshot_and_unknown_mergeability_block(self):
        for change in (
            lambda s: s.update(stable=False),
            lambda s: s["complete"].pop("threads"),
            lambda s: s["pr"].update(mergeable=None),
            lambda s: s["pr"]["base"].update(sha="f" * 40),
        ):
            snap = copy.deepcopy(self.snapshot)
            change(snap)
            self.assertFalse(gate.evaluate(snap, self.policy)["allow"])

    def test_malformed_ledger_does_not_fall_back_to_prior_good_comment(self):
        self.snapshot["issue_comments"].append(
            {
                "id": 1001,
                "user": AUTHOR,
                "updated_at": LATER,
                "html_url": "https://github.com/example/integration/pull/1#issuecomment-1001",
                "body": "<!-- " + gate.LEDGER_MARKER + " {broken} -->",
            }
        )
        self.assertIn("invalid_evidence", codes(self.snapshot, self.policy))

    def test_older_malformed_ledger_is_not_ignored(self):
        self.snapshot["issue_comments"].insert(
            0,
            {
                "id": 900,
                "user": AUTHOR,
                "updated_at": WHEN,
                "html_url": "https://github.com/example/integration/pull/1#issuecomment-900",
                "body": "<!-- " + gate.LEDGER_MARKER + " {broken} -->",
            },
        )
        self.assertIn("invalid_evidence", codes(self.snapshot, self.policy))

    def test_duplicate_keys_in_marker_rejected(self):
        with self.assertRaises(gate.GateError):
            gate.marker(
                '<!-- hal-feedback-ledger:v1 {"schema":1,"schema":2} -->',
                gate.LEDGER_MARKER,
            )

    def test_check_output_annotations_change_invalidates_ledger(self):
        self.snapshot["checks"][0]["_annotations"] = [
            {"message": "New serious warning"}
        ]
        self.snapshot["checks"][0]["output"]["annotations_count"] = 1
        self.assertIn("feedback_review_stale", codes(self.snapshot, self.policy))

    def test_queued_workflow_without_emitted_checks_blocks(self):
        queued = copy.deepcopy(self.snapshot["workflows"][0])
        queued.update(id=21, run_number=6, status="queued", conclusion=None, _jobs=[])
        self.snapshot["workflows"].append(queued)
        self.assertIn(
            "latest_workflow_not_successful", codes(self.snapshot, self.policy)
        )

    def test_pending_review_and_malformed_completed_review_block(self):
        self.snapshot["reviews"][0]["state"] = "PENDING"
        self.snapshot["review_meta"][0]["state"] = "PENDING"
        seal(self.snapshot, self.policy)
        self.assertIn("review_not_completed", codes(self.snapshot, self.policy))
        self.snapshot["reviews"][0]["state"] = "COMMENTED"
        self.snapshot["review_meta"][0]["state"] = "COMMENTED"
        self.snapshot["reviews"][0]["submitted_at"] = "not-a-time"
        self.assertIn("invalid_evidence", codes(self.snapshot, self.policy))

    def test_unknown_policy_field_is_never_silently_ignored(self):
        self.policy["allow_failed_checks"] = True
        self.assertIn("invalid_evidence", codes(self.snapshot, self.policy))


class ClientTests(unittest.TestCase):
    def full_transport(self, snapshot, policy):
        """Fake the genuine REST + GraphQL response shapes, not the gate API."""

        def transport(url, query):
            if query:
                source = query["query"]
                if "reviewThreads(first:" in source:
                    nodes = copy.deepcopy(snapshot["threads"])
                    for thread in nodes:
                        thread["comments"] = {
                            "nodes": thread["comments"],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    field = "reviewThreads"
                elif "comments(first:" in source:
                    field = "comments"
                    nodes = [
                        comment.get("_handling_review_meta")
                        or comment.get("_native_review_meta")
                        for comment in snapshot["issue_comments"]
                        if "_handling_review_meta" in comment
                        or "_native_review_meta" in comment
                    ]
                else:
                    field, nodes = "reviews", snapshot["review_meta"]
                return {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                field: {
                                    "nodes": copy.deepcopy(nodes),
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                }, {}
            path = url.split(f"/repos/{REPOSITORY}/", 1)[1]
            if path == "git/ref/heads/main":
                return {
                    "ref": "refs/heads/main",
                    "object": {"sha": BASE, "type": "commit"},
                }, {}
            if path.startswith("contents/"):
                self.assertEqual(path, f"contents/{gate.POLICY_PATH}?ref={BASE}")
                return {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(json.dumps(policy).encode()).decode(),
                }, {}
            if path == "pulls/1":
                return copy.deepcopy(snapshot["pr"]), {}
            if path.startswith("compare/"):
                return copy.deepcopy(snapshot["comparison"]), {}
            if path.startswith("issues/1/comments?"):
                return [
                    {
                        k: copy.deepcopy(v)
                        for k, v in comment.items()
                        if k not in {"_handling_review_meta", "_native_review_meta"}
                    }
                    for comment in snapshot["issue_comments"]
                ], {}
            if path.startswith("pulls/1/reviews?"):
                return copy.deepcopy(snapshot["reviews"]), {}
            if path.startswith("pulls/1/comments?"):
                return copy.deepcopy(snapshot["review_comments"]), {}
            if "/check-runs?" in path:
                values = [
                    {k: v for k, v in c.items() if not k.startswith("_")}
                    for c in snapshot["checks"]
                ]
                return {
                    "total_count": len(values),
                    "check_runs": copy.deepcopy(values),
                }, {}
            if "/statuses?" in path:
                return copy.deepcopy(snapshot["statuses"]), {}
            if path.startswith("actions/runs?"):
                values = [
                    {k: v for k, v in r.items() if not k.startswith("_")}
                    for r in snapshot["workflows"]
                ]
                return {
                    "total_count": len(values),
                    "workflow_runs": copy.deepcopy(values),
                }, {}
            if path.startswith("actions/runs/") and "/attempts/1/jobs?" in path:
                run = next(
                    run
                    for run in snapshot["workflows"]
                    if run["id"] == int(path.split("/")[2])
                )
                return {
                    "total_count": len(run["_jobs"]),
                    "jobs": copy.deepcopy(run["_jobs"]),
                }, {}
            if "/check-suites?" in path:
                return {
                    "total_count": len(snapshot["check_suites"]),
                    "check_suites": copy.deepcopy(snapshot["check_suites"]),
                }, {}
            self.fail(f"unexpected read route: {path}")

        return transport

    def test_full_collector_fetches_base_policy_two_sweeps_and_gates(self):
        snapshot, policy = fixture()
        client = gate.GitHub(
            REPOSITORY, "fixture-placeholder", self.full_transport(snapshot, policy)
        )
        collected, actual_policy = client.collect(1)
        self.assertTrue(collected["stable"])
        self.assertEqual(actual_policy, policy)
        # The fixture ledger fingerprints the same raw data enriched by collection.
        self.assertTrue(
            gate.evaluate(collected, policy)["allow"], gate.evaluate(collected, policy)
        )

    def test_full_collector_detects_comment_update_between_sweeps(self):
        snapshot, policy = fixture()
        base_transport = self.full_transport(snapshot, policy)
        count = 0

        def transport(url, query):
            nonlocal count
            response = base_transport(url, query)
            if "/issues/1/comments?" in url:
                count += 1
                if count == 2:
                    response[0][0]["body"] = "New finding appeared while collecting."
            return response

        with self.assertRaisesRegex(gate.GateError, "between complete reads"):
            gate.GitHub(REPOSITORY, "fixture-placeholder", transport).collect(1)

    def test_custom_head_gate_check_is_excluded_without_workflow_mapping(self):
        snapshot, policy = fixture()
        policy["gate_check"] = {
            "app_id": 15368,
            "workflow_path": None,
            "name": "Autonomous maintenance / merge gate",
        }
        own = copy.deepcopy(snapshot["checks"][0])
        own.update(
            id=999,
            name=policy["gate_check"]["name"],
            status="in_progress",
            conclusion=None,
            completed_at=None,
        )
        own["_workflow"] = None
        snapshot["checks"].append(own)
        seal(snapshot, policy)
        attest(snapshot, policy)
        client = gate.GitHub(
            REPOSITORY, "fixture-placeholder", self.full_transport(snapshot, policy)
        )
        collected, actual_policy = client.collect(1)
        result = gate.evaluate(collected, actual_policy)
        self.assertTrue(result["allow"], result)
        self.assertNotIn("check:999", {x["source"] for x in result["feedback"]})

    def test_nested_thread_comment_pagination(self):
        calls = []

        def transport(url, query):
            calls.append(query["variables"])
            after = query["variables"]["after"]
            return {
                "data": {
                    "node": {
                        "comments": {
                            "nodes": [{"databaseId": 2 if after else 1}],
                            "pageInfo": {
                                "hasNextPage": after is None,
                                "endCursor": "next",
                            },
                        }
                    }
                }
            }, {}

        client = gate.GitHub(REPOSITORY, "fixture-placeholder", transport)
        result = client.graphql_pages(1, "comments", "databaseId", thread_id="thread1")
        self.assertEqual(len(result), 2)
        self.assertEqual(calls[1]["id"], "thread1")

    def test_rest_pagination_including_later_feedback(self):
        calls = []

        def transport(url, query):
            calls.append(url)
            if len(calls) == 1:
                return [{"id": 1}], {
                    "Link": '<https://api.github.com/repos/example/integration/issues/1/comments?page=2>; rel="next"'
                }
            return [{"id": 2}], {}

        client = gate.GitHub(REPOSITORY, "fixture-placeholder", transport)
        self.assertEqual(
            client.pages("issues/1/comments?per_page=100"), [{"id": 1}, {"id": 2}]
        )

    def test_rest_pagination_rejects_truncation_loop_and_external_next_link(self):
        responses = [
            ({"total_count": 2, "check_runs": [{"id": 1}]}, {}),
            ([], {"Link": '<https://evil.test/steal>; rel="next"'}),
            (
                [],
                {
                    "Link": '<https://api.github.com/repos/example/integration/x>; rel="next"'
                },
            ),
        ]
        for response in responses:
            with self.subTest(response=response):
                client = gate.GitHub(
                    REPOSITORY,
                    "fixture-placeholder",
                    lambda u, q, response=response: response,
                )
                with self.assertRaises(gate.GateError):
                    client.pages(
                        "x", "check_runs" if isinstance(response[0], dict) else None
                    )

    def test_graphql_paginates_and_fails_on_partial_errors(self):
        def transport(url, query):
            after = query["variables"]["after"]
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviews": {
                                "nodes": [{"databaseId": 2 if after else 1}],
                                "pageInfo": {
                                    "hasNextPage": after is None,
                                    "endCursor": "next",
                                },
                            }
                        }
                    }
                }
            }, {}

        client = gate.GitHub(REPOSITORY, "fixture-placeholder", transport)
        self.assertEqual(len(client.graphql_pages(1, "reviews", "databaseId")), 2)
        client.transport = lambda u, q: (
            {"data": {}, "errors": [{"message": "denied"}]},
            {},
        )
        with self.assertRaises(gate.GateError):
            client.graphql_pages(1, "reviews", "databaseId")

    def test_client_rejects_mutations_and_cross_repo_urls(self):
        client = gate.GitHub(REPOSITORY, "fixture-placeholder", lambda u, q: ({}, {}))
        for path, query in (
            ("/graphql", {"query": "mutation {deleteIssue}"}),
            ("https://api.github.com/repos/other/repo/issues", None),
            ("https://evil.test/repos/example/integration/x", None),
        ):
            with self.assertRaises(gate.GateError):
                client.request(path, query)

    def test_offline_cli_emits_json_and_does_not_need_token(self):
        snapshot, policy = fixture()
        with tempfile.TemporaryDirectory() as folder:
            snap_path, policy_path = (
                Path(folder) / "snapshot.json",
                Path(folder) / "policy.json",
            )
            snap_path.write_text(json.dumps(snapshot))
            policy_path.write_text(json.dumps(policy))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = gate.main(
                    ["--snapshot", str(snap_path), "--policy", str(policy_path)]
                )
            self.assertEqual(status, 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["mode"], "offline-rehearsal")
            self.assertFalse(report["merge_authority"])
            self.assertFalse(report["validation_claimed"])


if __name__ == "__main__":
    unittest.main()
