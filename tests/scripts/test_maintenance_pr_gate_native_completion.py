"""Offline native-receipt contracts adapted from two captured real service outputs.

No live identity, GitHub action, reviewer invocation or generated service verdict.
"""

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

OBSERVED_RECEIPTS = [
    (
        "42ee1849d7c975faddc75a43b30ccf99c97d263b",
        (
            '<!-- codex-pull-request-review-summary -->\n<!-- codex-review-state:v1 {"code_rev'
            'iew":{"commit_sha":"42ee1849d7c975faddc75a43b30ccf99c97d263b","has_any_findings"'
            ':false,"has_unresolved_findings":false,"status":"completed"},"security_review":{'
            '"commit_sha":null,"has_any_findings":null,"has_unresolved_findings":null,"status'
            '":null}} -->\n\n## Codex Review Summary\n\nThis comment shows the latest Codex revie'
            "w activity on this pull request.\n\n| Review | Status | Commit | Review trigger |\n"
            "| --- | --- | --- | --- |\n| 📝 **Code Review** | ✅ **Completed** <relative-time d"
            'atetime="2026-11-01T02:27:49.709812Z">2026-11-01T02:27:49.709812Z</relative-time'
            "> | `42ee184` | New commits |\n\n\n\n<details> <summary>ℹ️ About Codex in GitHub</su"
            "mmary>\n<br/>\n\n[Your team has set up Codex to review pull requests in this repo]("
            "https://chatgpt.com/codex/cloud/settings/general). Reviews are triggered when yo"
            'u\n- Open a pull request for review\n- Mark a draft as ready\n- Comment "@codex rev'
            'iew" or "@codex security review".\n\nCodex reacts with 👀 while any review is runni'
            "ng, comments if it has suggestions, and reacts with 👍 once all reviews finish wi"
            "th no findings.\n\n</details>"
        ),
    ),
    (
        "007e994389af376af4bd9b5c2ae3af9ec33fc170",
        (
            '<!-- codex-pull-request-review-summary -->\n<!-- codex-review-state:v1 {"code_rev'
            'iew":{"commit_sha":"007e994389af376af4bd9b5c2ae3af9ec33fc170","has_any_findings"'
            ':false,"has_unresolved_findings":false,"status":"completed"},"security_review":{'
            '"commit_sha":null,"has_any_findings":null,"has_unresolved_findings":null,"status'
            '":null}} -->\n\n## Codex Review Summary\n\nThis comment shows the latest Codex revie'
            "w activity on this pull request.\n\n| Review | Status | Commit | Review trigger |\n"
            "| --- | --- | --- | --- |\n| 📝 **Code Review** | ✅ **Completed** <relative-time d"
            'atetime="2026-11-01T02:29:03.467737Z">2026-11-01T02:29:03.467737Z</relative-time'
            "> | `007e994` | New commits |\n\n\n\n<details> <summary>ℹ️ About Codex in GitHub</su"
            "mmary>\n<br/>\n\n[Your team has set up Codex to review pull requests in this repo]("
            "https://chatgpt.com/codex/cloud/settings/general). Reviews are triggered when yo"
            'u\n- Open a pull request for review\n- Mark a draft as ready\n- Comment "@codex rev'
            'iew" or "@codex security review".\n\nCodex reacts with 👀 while any review is runni'
            "ng, comments if it has suggestions, and reacts with 👍 once all reviews finish wi"
            "th no findings.\n\n</details>"
        ),
    ),
]


def receipt(snapshot, *, observed=0):
    original_head, original_body = OBSERVED_RECEIPTS[observed]
    comment = {
        "id": 80,
        "user": copy.deepcopy(fixtures.CODEX),
        "body": original_body.replace(original_head, fixtures.HEAD),
        "created_at": fixtures.WHEN,
        "updated_at": fixtures.LATER,
        "html_url": "https://github.com/example/integration/pull/1#issuecomment-80",
    }
    actor = {
        "__typename": "Bot",
        "databaseId": fixtures.CODEX["id"],
        "login": fixtures.CODEX["login"].removesuffix("[bot]"),
    }
    comment["_native_review_meta"] = {
        "databaseId": 80,
        "body": comment["body"],
        "updatedAt": comment["updated_at"],
        "lastEditedAt": comment["updated_at"],
        "includesCreatedEdit": True,
        "author": actor,
        "editor": copy.deepcopy(actor),
    }
    snapshot["issue_comments"].append(comment)
    return comment


def rewrite(comment, changes=None, *, body=None):
    if body is None:
        lines = comment["body"].splitlines()
        state = json.loads(
            lines[1].removeprefix("<!-- codex-review-state:v1 ").removesuffix(" -->")
        )
        changes(state)
        lines[1] = "<!-- codex-review-state:v1 " + json.dumps(state) + " -->"
        body = "\n".join(lines)
    comment["body"] = body
    comment["_native_review_meta"]["body"] = body


def remove_codex_formal(snapshot):
    snapshot["reviews"] = [r for r in snapshot["reviews"] if r["id"] != 30]
    snapshot["review_meta"] = [
        r for r in snapshot["review_meta"] if r["databaseId"] != 30
    ]


def refresh(snapshot, policy):
    fixtures.seal(snapshot, policy)
    fixtures.attest(snapshot, policy)
    return gate.evaluate(snapshot, policy)


class NativeCompletionTests(unittest.TestCase):
    def fallback(self, observed=0):
        snapshot, policy = fixtures.fixture()
        remove_codex_formal(snapshot)
        comment = receipt(snapshot, observed=observed)
        return snapshot, policy, comment

    def test_both_real_completed_clean_receipts_qualify_and_remain_feedback(self):
        for observed in range(2):
            with self.subTest(observed=observed):
                snapshot, policy, comment = self.fallback(observed)
                result = refresh(snapshot, policy)
                self.assertTrue(result["allow"], result)
                self.assertEqual(
                    result["codex_review"]["kind"], "issue_comment_summary"
                )
                self.assertEqual(result["codex_review"]["id"], comment["id"])
                self.assertIn(
                    "issue_comment:80", {f["source"] for f in result["feedback"]}
                )
                self.assertEqual(
                    result["handling_evidence"]["source"], fixtures.WITNESS_SOURCE
                )

    def test_unedited_bot_receipt_is_valid_but_missing_editor_fields_are_not(self):
        snapshot, policy, comment = self.fallback()
        comment["created_at"] = comment["updated_at"]
        comment["_native_review_meta"].update(
            editor=None, lastEditedAt=None, includesCreatedEdit=False
        )
        self.assertTrue(refresh(snapshot, policy)["allow"])
        del comment["_native_review_meta"]["editor"]
        self.assertFalse(refresh(snapshot, policy)["allow"])

    def test_absent_editor_cannot_certify_updated_or_invalid_creation_time(self):
        for created in (fixtures.WHEN, None, "not-a-timestamp"):
            with self.subTest(created=created):
                snapshot, policy, comment = self.fallback()
                comment["created_at"] = created
                comment["_native_review_meta"].update(
                    editor=None, lastEditedAt=None, includesCreatedEdit=False
                )
                self.assertFalse(refresh(snapshot, policy)["allow"])

    def test_receipt_never_replaces_independent_handling_proof(self):
        snapshot, policy, _ = self.fallback()
        refresh(snapshot, policy)
        fixtures.drop_handling_witness(snapshot)
        fixtures.seal(snapshot, policy)
        result = gate.evaluate(snapshot, policy)
        self.assertEqual(result["codex_review"]["kind"], "issue_comment_summary")
        self.assertFalse(result["allow"])
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            fixtures.codes(snapshot, policy),
        )

    def test_reactions_or_author_copies_do_not_establish_review(self):
        snapshot, policy = fixtures.fixture()
        remove_codex_formal(snapshot)
        snapshot["reactions"] = [{"content": "+1", "user": fixtures.CODEX}]
        self.assertFalse(refresh(snapshot, policy)["allow"])
        comment = receipt(snapshot)
        for changes in (
            {"id": fixtures.AUTHOR["id"]},
            {"login": "someone-else[bot]"},
            fixtures.AUTHOR,
        ):
            original = comment["user"]
            comment["user"] = {**original, **changes}
            self.assertFalse(refresh(snapshot, policy)["allow"])
            comment["user"] = original
        policy["codex_reviewers"] = [fixtures.AUTHOR]
        comment["user"] = copy.deepcopy(fixtures.AUTHOR)
        self.assertFalse(refresh(snapshot, policy)["allow"])

    def test_current_formal_completion_remains_accepted(self):
        snapshot, policy = fixtures.fixture()
        self.assertTrue(gate.evaluate(snapshot, policy)["allow"])
        comment = receipt(snapshot)
        rewrite(comment, lambda s: s["code_review"].update(has_any_findings=True))
        result = refresh(snapshot, policy)
        self.assertTrue(result["allow"], result)
        self.assertEqual(result["codex_review"]["id"], 30)

    def test_pending_or_dismissed_formal_cannot_be_rescued_by_clean_receipt(self):
        for state in ("PENDING", "DISMISSED", "CHANGES_REQUESTED"):
            snapshot, policy = fixtures.fixture()
            receipt(snapshot)
            snapshot["reviews"][0]["state"] = state
            snapshot["review_meta"][0]["state"] = state
            self.assertFalse(refresh(snapshot, policy)["allow"])

    def test_wrong_head_running_adverse_or_non_boolean_code_state_never_qualifies(self):
        changes = [
            {"commit_sha": "c" * 40},
            {"commit_sha": "a" * 7},
            {"commit_sha": None},
            {"status": "requested"},
            {"status": "running"},
            {"status": "in_progress"},
            {"status": "cancelled"},
            {"status": None},
            {"status": "future-state"},
            {"status": 3},
            {"has_any_findings": True},
            {"has_unresolved_findings": True},
            {"has_any_findings": 0},
            {"has_unresolved_findings": "false"},
        ]
        for change in changes:
            with self.subTest(change=change):
                snapshot, policy, comment = self.fallback()
                rewrite(
                    comment, lambda s, change=change: s["code_review"].update(change)
                )
                self.assertFalse(refresh(snapshot, policy)["allow"])

    def test_security_must_be_unrequested_or_current_completed_clean(self):
        clean = {
            "commit_sha": fixtures.HEAD,
            "status": "completed",
            "has_any_findings": False,
            "has_unresolved_findings": False,
        }
        snapshot, policy, comment = self.fallback()
        rewrite(comment, lambda s: s.update(security_review=clean))
        self.assertTrue(refresh(snapshot, policy)["allow"])
        for change in (
            {"status": "requested", "commit_sha": None},
            {"status": "running"},
            {"commit_sha": "c" * 40},
            {"has_any_findings": True},
            {"has_unresolved_findings": True},
            {"has_any_findings": None},
            {"status": "future-state"},
        ):
            with self.subTest(change=change):
                snapshot, policy, comment = self.fallback()
                rewrite(
                    comment,
                    lambda s, change=change: s.update(
                        security_review={**clean, **change}
                    ),
                )
                self.assertFalse(refresh(snapshot, policy)["allow"])
                # A completed code formal review cannot hide pending/adverse security work.
                original, _ = fixtures.fixture()
                snapshot["reviews"] = original["reviews"]
                snapshot["review_meta"] = original["review_meta"]
                self.assertFalse(refresh(snapshot, policy)["allow"])

    def test_unknown_or_malformed_schema_and_quoted_markers_fail_closed(self):
        for change in (
            lambda s: s.update(extra=True),
            lambda s: s.pop("security_review"),
            lambda s: s["code_review"].update(extra=False),
            lambda s: s["code_review"].pop("status"),
            lambda s: s.update(code_review=[]),
            lambda s: s.update(security_review=None),
        ):
            snapshot, policy, comment = self.fallback()
            rewrite(comment, change)
            self.assertFalse(refresh(snapshot, policy)["allow"])
        for change in (
            lambda b: b.replace("codex-review-state:v1", "codex-review-state:v2"),
            lambda b: b.replace(
                '"has_any_findings":false',
                '"has_any_findings":false,"has_any_findings":true',
                1,
            ),
            lambda b: b.replace('"code_review":{', '"code_review":{broken', 1),
            lambda b: "```\n" + b + "\n```",
            lambda b: b + "\n" + b.splitlines()[1],
        ):
            snapshot, policy, comment = self.fallback()
            rewrite(comment, body=change(comment["body"]))
            self.assertFalse(refresh(snapshot, policy)["allow"])

    def test_rest_graphql_identity_body_time_and_editor_must_agree(self):
        for change in (
            lambda c: c.pop("_native_review_meta"),
            lambda c: c["_native_review_meta"].update(databaseId=999),
            lambda c: c["_native_review_meta"].update(databaseId=True),
            lambda c: c["_native_review_meta"].update(body="forged clean body"),
            lambda c: c["_native_review_meta"].update(updatedAt=fixtures.WHEN),
            lambda c: c["_native_review_meta"].update(lastEditedAt=fixtures.WHEN),
            lambda c: c["_native_review_meta"].update(lastEditedAt=None),
            lambda c: c["_native_review_meta"].update(editor=None),
            lambda c: c["_native_review_meta"]["editor"].update(
                databaseId=fixtures.AUTHOR["id"]
            ),
            lambda c: c["_native_review_meta"]["editor"].update(__typename="User"),
            lambda c: c["_native_review_meta"]["author"].update(login="forged"),
        ):
            snapshot, policy, comment = self.fallback()
            change(comment)
            self.assertFalse(refresh(snapshot, policy)["allow"])

    def test_summary_edits_invalidate_ledger_and_handling_at_fixed_timestamp(self):
        snapshot, policy, comment = self.fallback()
        self.assertTrue(refresh(snapshot, policy)["allow"])
        stamp = comment["updated_at"]
        rewrite(comment, body=comment["body"] + "\nAn additional bot report.")
        result = gate.evaluate(snapshot, policy)
        self.assertEqual(stamp, comment["updated_at"])
        self.assertFalse(result["allow"])
        fixtures.seal(snapshot, policy)
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            fixtures.codes(snapshot, policy),
        )
        self.assertTrue(refresh(snapshot, policy)["allow"])

    def test_duplicate_summary_cannot_hide_later_inflight_or_adverse_state(self):
        snapshot, policy, _ = self.fallback()
        later = receipt(snapshot)
        later["id"] = 81
        later["_native_review_meta"]["databaseId"] = 81
        rewrite(later, lambda s: s["code_review"].update(status="running"))
        self.assertFalse(refresh(snapshot, policy)["allow"])

    def transport(self, snapshot, policy, mutate_editor=False):
        normal = fixtures.ClientTests().full_transport(snapshot, policy)
        counts = {"meta": 0}

        def call(url, query):
            if (
                query
                and "comments(first:" in query["query"]
                and "reviewThreads" not in query["query"]
            ):
                counts["meta"] += 1
                nodes = [
                    copy.deepcopy(
                        c.get("_handling_review_meta") or c.get("_native_review_meta")
                    )
                    for c in snapshot["issue_comments"]
                    if "_native_review_meta" in c or "_handling_review_meta" in c
                ]
                if mutate_editor and counts["meta"] == 2:
                    next(node for node in nodes if node["databaseId"] == 80)["editor"][
                        "databaseId"
                    ] = fixtures.AUTHOR["id"]
                return {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "comments": {
                                    "nodes": nodes,
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                }, {}
            data, headers = normal(url, query)
            if not query and "/issues/1/comments?" in url:
                for item in data:
                    item.pop("_native_review_meta", None)
            return data, headers

        return call, counts

    def test_live_collector_cross_reads_real_editor_fields_and_fingerprints_them(self):
        snapshot, policy, _ = self.fallback()
        refresh(snapshot, policy)
        transport, counts = self.transport(snapshot, policy)
        collected, actual = gate.GitHub(
            fixtures.REPOSITORY, "fixture", transport
        ).collect(1)
        self.assertEqual(counts["meta"], 2)
        self.assertTrue(gate.evaluate(collected, actual)["allow"])
        transport, _ = self.transport(snapshot, policy, mutate_editor=True)
        with self.assertRaisesRegex(gate.GateError, "changed between complete reads"):
            gate.GitHub(fixtures.REPOSITORY, "fixture", transport).collect(1)

    def test_missing_or_duplicate_graphql_comment_metadata_blocks_collection(self):
        for duplicate in (False, True):
            snapshot, policy, _ = self.fallback()
            refresh(snapshot, policy)
            normal, _ = self.transport(snapshot, policy)

            def transport(url, query, normal=normal, duplicate=duplicate):
                data, headers = normal(url, query)
                if (
                    query
                    and "comments(first:" in query["query"]
                    and "reviewThreads" not in query["query"]
                ):
                    nodes = data["data"]["repository"]["pullRequest"]["comments"][
                        "nodes"
                    ]
                    if duplicate:
                        nodes.append(copy.deepcopy(nodes[0]))
                    else:
                        nodes.clear()
                return data, headers

            with self.assertRaisesRegex(gate.GateError, "metadata"):
                gate.GitHub(fixtures.REPOSITORY, "fixture", transport).collect(1)
