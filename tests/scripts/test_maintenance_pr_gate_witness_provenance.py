"""Clean witness authority follows the original independent actor's edits."""

from __future__ import annotations

import copy
import json
import unittest

import test_maintenance_pr_gate as fixtures

gate = fixtures.gate


def graphql_actor(actor):
    bot = actor["login"].endswith("[bot]")
    return {
        "__typename": "Bot" if bot else "User",
        "databaseId": actor["id"],
        "login": actor["login"].removesuffix("[bot]") if bot else actor["login"],
    }


def provenance(comment, editor=None, *, edited=True):
    comment.setdefault("created_at", fixtures.WHEN)
    comment["_handling_review_meta"] = {
        "databaseId": comment["id"],
        "body": comment["body"],
        "updatedAt": comment["updated_at"],
        "lastEditedAt": comment["updated_at"] if edited else None,
        "includesCreatedEdit": edited,
        "author": graphql_actor(comment["user"]),
        "editor": graphql_actor(editor or comment["user"]) if edited else None,
    }


def witness(snapshot):
    return next(c for c in snapshot["issue_comments"] if c["id"] == fixtures.WITNESS_ID)


class WitnessProvenanceTests(unittest.TestCase):
    def test_shared_helper_does_not_allow_user_authored_native_summary(self):
        import test_maintenance_pr_gate_native_completion as native

        snapshot, _ = fixtures.fixture()
        comment = native.receipt(snapshot)
        comment["user"] = copy.deepcopy(fixtures.INDEPENDENT)
        comment["_native_review_meta"].update(
            author=graphql_actor(fixtures.INDEPENDENT),
            editor=graphql_actor(fixtures.INDEPENDENT),
        )
        with self.assertRaises(gate.GateError):
            gate.native_summary_state(comment)

    def authenticated_fixture(self, actor):
        snapshot, policy = fixtures.fixture()
        policy["independent_reviewers"] = [actor]
        comment = witness(snapshot)
        comment["user"] = copy.deepcopy(actor)
        fixtures.seal(snapshot, policy)
        fixtures.attest(snapshot, policy)
        provenance(comment)
        fixtures.seal(snapshot, policy)
        return snapshot, policy, comment

    def test_owner_cannot_rewrite_original_bot_witness_to_launder_dispositions(self):
        snapshot, policy = fixtures.fixture()
        policy["independent_reviewers"] = [fixtures.CODEX]
        comment = witness(snapshot)
        comment["user"] = copy.deepcopy(fixtures.CODEX)
        fixtures.seal(snapshot, policy)
        fixtures.attest(snapshot, policy)
        provenance(comment)
        fixtures.seal(snapshot, policy)
        self.assertTrue(gate.evaluate(snapshot, policy)["allow"])

        overrides = {
            "issue_comment:10": {
                "reason": "Repository writer changed the reviewed disposition."
            }
        }
        fixtures.seal(snapshot, policy, overrides)
        blocked = gate.evaluate(snapshot, policy)
        self.assertFalse(blocked["allow"])
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            fixtures.codes(snapshot, policy),
        )

        expected = next(
            c
            for c in blocked["handling_candidates"]
            if c["source"] == fixtures.WITNESS_SOURCE
        )
        verdict = gate.marker(comment["body"], gate.CLEAN_MARKER)
        verdict.update(
            {key: expected[key] for key in ("feedback_digest", "dispositions_digest")}
        )
        comment["body"] = (
            "<!-- " + gate.CLEAN_MARKER + " " + json.dumps(verdict) + " -->"
        )
        provenance(comment, fixtures.AUTHOR)
        fixtures.seal(snapshot, policy, overrides)
        self.assertEqual(fixtures.CODEX, comment["user"])
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])

    def test_genuine_original_bot_and_user_edits_can_reauthorize_changed_handling(self):
        for actor in (fixtures.CODEX, fixtures.INDEPENDENT):
            snapshot, policy, comment = self.authenticated_fixture(actor)
            self.assertTrue(gate.evaluate(snapshot, policy)["allow"])
            fixtures.seal(
                snapshot,
                policy,
                {
                    "issue_comment:10": {
                        "reason": "A changed disposition needs a new independent decision."
                    }
                },
            )
            self.assertFalse(gate.evaluate(snapshot, policy)["allow"])
            fixtures.attest(snapshot, policy)
            self.assertEqual(
                graphql_actor(actor), comment["_handling_review_meta"]["editor"]
            )
            self.assertTrue(gate.evaluate(snapshot, policy)["allow"])

    def test_author_ledger_author_and_other_trusted_reviewer_cannot_edit_witness(self):
        pr_author = {"id": 2, "login": "pull-author"}
        other = {"id": 5, "login": "another-reviewer"}
        for editor in (pr_author, fixtures.AUTHOR, other):
            snapshot, policy, comment = self.authenticated_fixture(fixtures.INDEPENDENT)
            snapshot["pr"]["user"] = pr_author
            policy["independent_reviewers"].append(other)
            fixtures.seal(snapshot, policy)
            fixtures.attest(snapshot, policy)
            self.assertTrue(gate.evaluate(snapshot, policy)["allow"])
            provenance(comment, editor)
            fixtures.seal(snapshot, policy)
            with self.subTest(editor=editor):
                self.assertFalse(gate.evaluate(snapshot, policy)["allow"])

    def test_never_edited_witness_requires_equal_creation_time_and_null_editor(self):
        for actor in (fixtures.CODEX, fixtures.INDEPENDENT):
            snapshot, policy, comment = self.authenticated_fixture(actor)
            comment["created_at"] = comment["updated_at"]
            provenance(comment, edited=False)
            fixtures.seal(snapshot, policy)
            self.assertTrue(gate.evaluate(snapshot, policy)["allow"])
            comment["created_at"] = fixtures.WHEN
            fixtures.seal(snapshot, policy)
            self.assertFalse(gate.evaluate(snapshot, policy)["allow"])

    def test_missing_mismatched_and_ambiguous_provenance_never_authorizes(self):
        changes = [
            lambda c: c.pop("_handling_review_meta"),
            lambda c: c["_handling_review_meta"].pop("editor"),
            lambda c: c["_handling_review_meta"].update(databaseId=True),
            lambda c: c["_handling_review_meta"].update(databaseId=900),
            lambda c: c["_handling_review_meta"].update(body="Different bytes"),
            lambda c: c["_handling_review_meta"].update(updatedAt=fixtures.WHEN),
            lambda c: c["_handling_review_meta"].update(includesCreatedEdit=None),
            lambda c: c["_handling_review_meta"]["author"].update(databaseId=999),
            lambda c: c["_handling_review_meta"]["author"].update(login="different"),
            lambda c: c["_handling_review_meta"]["author"].update(__typename="User"),
            lambda c: c["_handling_review_meta"].update(editor=None),
            lambda c: c["_handling_review_meta"].update(lastEditedAt=None),
            lambda c: c["_handling_review_meta"].update(lastEditedAt=fixtures.WHEN),
            lambda c: c["_handling_review_meta"]["editor"].update(databaseId=True),
            lambda c: c["_handling_review_meta"]["editor"].update(login="different"),
        ]
        for change in changes:
            snapshot, policy, comment = self.authenticated_fixture(fixtures.CODEX)
            change(comment)
            fixtures.seal(snapshot, policy)
            with self.subTest(change=change):
                self.assertFalse(gate.evaluate(snapshot, policy)["allow"])


class WitnessProvenanceCollectorTests(unittest.TestCase):
    def test_untrusted_author_marker_is_not_fetched_or_used_as_independent_proof(self):
        snapshot, policy = fixtures.fixture()
        comment = witness(snapshot)
        comment["user"] = copy.deepcopy(fixtures.AUTHOR)
        fixtures.seal(snapshot, policy)
        ordinary = fixtures.ClientTests().full_transport(snapshot, policy)
        reads = []

        def transport(url, query):
            if (
                query
                and "comments(first:" in query["query"]
                and "reviewThreads" not in query["query"]
            ):
                reads.append(query)
            return ordinary(url, query)

        collected, actual = gate.GitHub(
            fixtures.REPOSITORY, "placeholder", transport
        ).collect(1)
        self.assertEqual([], reads)
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            fixtures.codes(collected, actual),
        )

    def test_each_sweep_fetches_current_witness_author_and_editor_metadata(self):
        snapshot, policy = fixtures.fixture()
        ordinary = fixtures.ClientTests().full_transport(snapshot, policy)
        reads = []

        def transport(url, query):
            if (
                query
                and "comments(first:" in query["query"]
                and "reviewThreads" not in query["query"]
            ):
                reads.append(query["query"])
                self.assertIn("lastEditedAt", query["query"])
                self.assertIn("editor{__typename login", query["query"])
            return ordinary(url, query)

        collected, actual = gate.GitHub(
            fixtures.REPOSITORY, "placeholder", transport
        ).collect(1)
        self.assertEqual(2, len(reads))
        self.assertEqual(
            witness(snapshot)["_handling_review_meta"],
            witness(collected)["_handling_review_meta"],
        )
        self.assertTrue(gate.evaluate(collected, actual)["allow"])

    def test_absent_duplicate_and_moving_graphql_proof_blocks_collection(self):
        for mode in ("absent", "duplicate", "moving"):
            snapshot, policy = fixtures.fixture()
            ordinary = fixtures.ClientTests().full_transport(snapshot, policy)
            reads = 0

            def transport(url, query, mode=mode, ordinary=ordinary):
                nonlocal reads
                data, headers = ordinary(url, query)
                if (
                    query
                    and "comments(first:" in query["query"]
                    and "reviewThreads" not in query["query"]
                ):
                    reads += 1
                    nodes = data["data"]["repository"]["pullRequest"]["comments"][
                        "nodes"
                    ]
                    if mode == "absent":
                        nodes.clear()
                    elif mode == "duplicate":
                        nodes.append(copy.deepcopy(nodes[0]))
                    elif reads == 2:
                        nodes[0].update(
                            lastEditedAt=nodes[0]["updatedAt"],
                            editor=graphql_actor(fixtures.AUTHOR),
                            includesCreatedEdit=True,
                        )
                return data, headers

            with self.subTest(mode=mode), self.assertRaises(gate.GateError):
                gate.GitHub(fixtures.REPOSITORY, "placeholder", transport).collect(1)

    def test_rest_injected_old_proof_cannot_replace_fresh_graphql_human_editor(self):
        snapshot, policy = fixtures.fixture()
        old_proof = copy.deepcopy(witness(snapshot)["_handling_review_meta"])
        provenance(witness(snapshot), fixtures.AUTHOR)
        fixtures.seal(snapshot, policy)
        ordinary = fixtures.ClientTests().full_transport(snapshot, policy)

        def transport(url, query):
            data, headers = ordinary(url, query)
            if not query and "/issues/1/comments?" in url:
                witness({"issue_comments": data})["_handling_review_meta"] = old_proof
            return data, headers

        collected, actual = gate.GitHub(
            fixtures.REPOSITORY, "placeholder", transport
        ).collect(1)
        self.assertEqual(
            graphql_actor(fixtures.AUTHOR),
            witness(collected)["_handling_review_meta"]["editor"],
        )
        self.assertFalse(gate.evaluate(collected, actual)["allow"])


class WitnessRecoveryOrderTests(unittest.TestCase):
    def recovery(self, ident, corrupt=None, editor=None):
        snapshot, policy = fixtures.fixture()
        old = witness(snapshot)
        verdict = gate.marker(old["body"], gate.CLEAN_MARKER)
        verdict["reason"] = (
            "Old witness was edited; a fresh independent review must cover it."
        )
        old["body"] = "<!-- " + gate.CLEAN_MARKER + " " + json.dumps(verdict) + " -->"
        provenance(old, editor or fixtures.AUTHOR)
        if corrupt is not None:
            corrupt(old)
        ledger = fixtures.seal(snapshot, policy)
        feedback, _, _ = gate.inventory(snapshot, policy)
        entries = {entry["source"]: entry for entry in ledger["entries"]}
        new = copy.deepcopy(old)
        new.update(
            id=ident,
            user=copy.deepcopy(fixtures.INDEPENDENT),
            html_url=f"https://github.com/example/integration/pull/1#issuecomment-{ident}",
        )
        verdict.update(
            reason="Fresh genuine independent review covers all sources, including the old witness.",
            feedback_digest=gate.inventory_digest(feedback),
            dispositions_digest=gate.digest(
                [entries[item["source"]] for item in feedback]
            ),
        )
        new["body"] = "<!-- " + gate.CLEAN_MARKER + " " + json.dumps(verdict) + " -->"
        provenance(new, fixtures.INDEPENDENT)
        snapshot["issue_comments"].append(new)
        fixtures.seal(snapshot, policy)
        return snapshot, policy

    def test_fresh_genuine_witness_recovers_in_both_source_orderings(self):
        for ident in (89, 91):
            for editor in (fixtures.AUTHOR, fixtures.CODEX):
                snapshot, policy = self.recovery(ident, editor=editor)
                result = gate.evaluate(snapshot, policy)
                with self.subTest(ident=ident, editor=editor):
                    self.assertTrue(result["allow"], result)
                    self.assertEqual(
                        f"issue_comment:{ident}", result["handling_evidence"]["source"]
                    )
                    self.assertIn(
                        fixtures.WITNESS_SOURCE,
                        {source["source"] for source in result["feedback"]},
                    )
                    _, ledger, _ = gate.inventory(snapshot, policy)
                    self.assertIn(
                        fixtures.WITNESS_SOURCE,
                        {entry["source"] for entry in ledger["entries"]},
                    )

    def test_missing_or_malformed_old_provenance_blocks_both_orderings(self):
        corruptions = [
            lambda c: c.pop("_handling_review_meta"),
            lambda c: c["_handling_review_meta"].update(editor=None),
            lambda c: c["_handling_review_meta"]["editor"].update(databaseId=True),
            lambda c: c["_handling_review_meta"]["editor"].update(
                __typename="Mannequin"
            ),
            lambda c: c["_handling_review_meta"]["editor"].update(login=""),
            lambda c: c["_handling_review_meta"]["editor"].update(login="not/a-login"),
            lambda c: c["_handling_review_meta"]["editor"].update(
                extra="not requested"
            ),
            lambda c: c["_handling_review_meta"].update(body="Contradictory body"),
            lambda c: c["_handling_review_meta"].update(lastEditedAt=fixtures.WHEN),
            lambda c: c["_handling_review_meta"]["editor"].update(
                databaseId=fixtures.INDEPENDENT["id"]
            ),
            lambda c: c["_handling_review_meta"]["editor"].update(
                login=fixtures.INDEPENDENT["login"]
            ),
        ]
        for ident in (89, 91):
            for corrupt in corruptions:
                snapshot, policy = self.recovery(ident, corrupt)
                with self.subTest(ident=ident, corrupt=corrupt):
                    self.assertFalse(gate.evaluate(snapshot, policy)["allow"])


if __name__ == "__main__":
    unittest.main()
