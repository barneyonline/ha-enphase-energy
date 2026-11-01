"""Actual branch-ref freshness independent of lagging PR metadata; no network."""

from __future__ import annotations

import copy
import unittest

try:
    import test_pr_gate as base
except ModuleNotFoundError:
    import test_maintenance_pr_gate as base

gate = base.gate
NEW = "c" * 40


class BaseRefTests(unittest.TestCase):
    def client(self, *, changed_at=None, malformed=None, gone_at=None):
        snapshot, policy = base.fixture()
        transport_base = base.ClientTests().full_transport(snapshot, policy)
        calls = []
        reads = 0

        def transport(url, query):
            nonlocal reads
            calls.append(url)
            if url.endswith("/git/ref/heads/main"):
                reads += 1
                if gone_at == reads:
                    raise gate.GateError("GitHub API HTTP 404")
                if malformed is not None:
                    return copy.deepcopy(malformed), {}
                sha = (
                    NEW if changed_at is not None and reads >= changed_at else base.BASE
                )
                return {
                    "ref": "refs/heads/main",
                    "object": {"sha": sha, "type": "commit"},
                }, {}
            return transport_base(url, query)

        return gate.GitHub(base.REPOSITORY, "fixture-only", transport), policy, calls

    def test_actual_branch_change_blocks_even_when_all_pr_responses_stay_old(self):
        client, _, calls = self.client(changed_at=1)
        with self.assertRaisesRegex(gate.GateError, "actual base branch ref disagrees"):
            client.collect(1)
        self.assertFalse(any("/contents/" in url for url in calls))

    def test_movement_after_policy_within_between_and_final_sweeps_blocks(self):
        # Reads: pre-policy; sweep1 start/end; sweep2 start/end; final recheck.
        for read in range(2, 7):
            client, _, _ = self.client(changed_at=read)
            with (
                self.subTest(read=read),
                self.assertRaisesRegex(gate.GateError, "actual base branch ref"),
            ):
                client.collect(1)

    def test_missing_disappeared_and_malformed_actual_refs_block(self):
        for ref in (
            {},
            [],
            {"object": None},
            {"ref": "refs/tags/main", "object": {"sha": base.BASE, "type": "commit"}},
            {"ref": "refs/heads/main", "object": {"sha": "bad", "type": "commit"}},
            {"ref": "refs/heads/main", "object": {"sha": base.BASE, "type": "tag"}},
        ):
            client, _, _ = self.client(malformed=ref)
            with self.subTest(ref=ref), self.assertRaises(gate.GateError):
                client.collect(1)
        for read in range(1, 7):
            client, _, _ = self.client(gone_at=read)
            with self.subTest(disappeared_at=read), self.assertRaises(gate.GateError):
                client.collect(1)

    def test_unchanged_actual_base_is_bound_to_every_stable_sweep(self):
        client, policy, calls = self.client()
        snapshot, actual_policy = client.collect(1)
        self.assertTrue(snapshot["stable"])
        self.assertEqual(actual_policy, policy)
        self.assertEqual(
            snapshot["base_ref"],
            gate.base_ref_proof(base.REPOSITORY, "main", base.BASE),
        )
        self.assertEqual(sum(url.endswith("/git/ref/heads/main") for url in calls), 6)
        self.assertTrue(gate.evaluate(snapshot, policy)["allow"])

    def test_pure_gate_rejects_missing_wrong_or_cross_repo_proof(self):
        for field, value in (
            (None, None),
            ("repository", "elsewhere/repo"),
            ("request_url", "https://elsewhere.invalid"),
            ("ref", "refs/tags/main"),
            ("object_type", "tag"),
            ("sha", NEW),
        ):
            snapshot, policy = base.fixture()
            if field is None:
                snapshot.pop("base_ref")
            else:
                snapshot["base_ref"][field] = value
            result = gate.evaluate(snapshot, policy)
            with self.subTest(field=field):
                self.assertFalse(result["allow"])
                self.assertIn(
                    "current_base_ref_unproven", {r["code"] for r in result["reasons"]}
                )

    def test_ref_evidence_participates_in_snapshot_stability(self):
        snapshot, policy = base.fixture()
        before = gate.digest(gate.decision_view(snapshot, policy))
        snapshot["base_ref"]["sha"] = NEW
        self.assertNotEqual(before, gate.digest(gate.decision_view(snapshot, policy)))

    def test_branch_path_escapes_slashes_and_refuses_traversal(self):
        self.assertEqual(
            gate.base_ref_path("maintenance/1.4"), "git/ref/heads/maintenance%2F1.4"
        )
        for branch in (
            "",
            "../main",
            "main/..",
            "/main",
            "main/",
            "main//other",
            "main\\other",
        ):
            with self.subTest(branch=branch), self.assertRaises(gate.GateError):
                gate.base_ref_path(branch)

    def test_policy_branch_mismatch_blocks_before_a_ref_request(self):
        snapshot, policy = base.fixture()
        client, _, calls = self.client()
        policy["base_branch"] = "different"
        with self.assertRaisesRegex(gate.GateError, "disagrees with trusted policy"):
            client.current_base_ref(snapshot["pr"], policy)
        self.assertEqual(calls, [])

    def test_consistent_new_pr_and_ref_still_reject_old_policy_selection(self):
        snapshot, policy = base.fixture()
        normal = base.ClientTests().full_transport(snapshot, policy)
        pulls = 0

        def transport(url, query):
            nonlocal pulls
            data, headers = normal(url, query)
            if url.endswith("/pulls/1"):
                pulls += 1
                if pulls >= 2:
                    data["base"]["sha"] = NEW
            if url.endswith("/git/ref/heads/main") and pulls >= 2:
                data["object"]["sha"] = NEW
            return data, headers

        with self.assertRaisesRegex(gate.GateError, "PR base changed after"):
            gate.GitHub(base.REPOSITORY, "fixture-only", transport).collect(1)

    def test_initial_target_branch_alias_change_is_not_hidden_by_equal_shas(self):
        snapshot, policy = base.fixture()
        normal = base.ClientTests().full_transport(snapshot, policy)
        pulls = 0

        def transport(url, query):
            nonlocal pulls
            if url.endswith("/git/ref/heads/alias"):
                return {
                    "ref": "refs/heads/alias",
                    "object": {"sha": base.BASE, "type": "commit"},
                }, {}
            data, headers = normal(url, query)
            if url.endswith("/pulls/1"):
                pulls += 1
                if pulls == 1:
                    data["base"]["ref"] = "alias"
            return data, headers

        with self.assertRaisesRegex(gate.GateError, "actual base branch changed after"):
            gate.GitHub(base.REPOSITORY, "fixture-only", transport).collect(1)


if __name__ == "__main__":
    unittest.main()
