"""Observed native final-answer envelope; fixtures confer no live authority."""

import copy
import unittest

import test_maintenance_pr_gate as base

gate = base.gate

FOOTER = "\n\n [View task →](https://chatgpt.com/s/cd_6ae6ba06771c819183f84fcfe73407c4)"
APP = {"id": 1144995, "slug": "chatgpt-codex-connector"}


def native_fixture():
    snapshot, policy = base.fixture()
    policy["independent_reviewers"] = [base.CODEX]
    witness = next(c for c in snapshot["issue_comments"] if c["id"] == base.WITNESS_ID)
    witness["user"] = {**base.CODEX, "type": "Bot"}
    witness["performed_via_github_app"] = copy.deepcopy(APP)
    base.comment_provenance(witness, edited=True)
    base.seal(snapshot, policy)
    base.attest(snapshot, policy)
    witness["body"] += FOOTER
    base.comment_provenance(witness, edited=True)
    base.seal(snapshot, policy)
    return snapshot, policy, witness


class NativeFooterTests(unittest.TestCase):
    def test_exact_service_footer_preserves_independent_acceptance(self):
        snapshot, policy, witness = native_fixture()
        result = gate.evaluate(snapshot, policy)
        self.assertTrue(result["allow"], result["reasons"])
        self.assertIn(FOOTER, witness["body"])
        self.assertIsNone(gate.marker(witness["body"], gate.CLEAN_MARKER))

    def test_other_comment_envelopes_do_not_become_witnesses(self):
        variants = [
            FOOTER.replace("chatgpt.com", "chatgpt.com.evil.invalid"),
            FOOTER.replace("https://", "http://"),
            FOOTER.replace("/s/cd_", "/s/other_"),
            FOOTER[:-1] + "?extra=1)",
            FOOTER[:-1] + "#fragment)",
            FOOTER + "\nExtra explanation",
            FOOTER + FOOTER,
            FOOTER.replace("View task →", "View task"),
        ]
        for footer in variants:
            with self.subTest(footer=footer):
                snapshot, policy, witness = native_fixture()
                witness["body"] = witness["body"].removesuffix(FOOTER) + footer
                base.comment_provenance(witness, edited=True)
                base.seal(snapshot, policy)
                self.assertFalse(gate.evaluate(snapshot, policy)["allow"])

    def test_wrapped_proof_requires_native_bot_and_application(self):
        changes = [
            {"user": {**base.CODEX, "type": "User"}},
            {"performed_via_github_app": None},
            {"performed_via_github_app": {**APP, "id": 99}},
            {"performed_via_github_app": {**APP, "slug": "another-app"}},
        ]
        for fields in changes:
            with self.subTest(fields=fields):
                snapshot, policy, witness = native_fixture()
                witness.update(fields)
                base.comment_provenance(witness, edited=True)
                base.seal(snapshot, policy)
                self.assertFalse(gate.evaluate(snapshot, policy)["allow"])
        snapshot, policy, witness = native_fixture()
        policy["codex_reviewers"] = [base.INDEPENDENT]
        base.seal(snapshot, policy)
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])

    def test_footer_does_not_skip_full_body_fingerprint_or_editor(self):
        snapshot, policy, witness = native_fixture()
        witness["body"] = witness["body"].replace(
            "6ae6ba06771c819183f84fcfe73407c4", "a" * 32
        )
        base.comment_provenance(witness, edited=True)
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])
        base.seal(snapshot, policy)
        base.comment_provenance(witness, edited=True, editor=base.AUTHOR)
        base.seal(snapshot, policy)
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])

    def test_owner_copy_and_quoted_certificate_cannot_gain_authority(self):
        snapshot, policy, witness = native_fixture()
        witness["user"] = {**base.AUTHOR, "type": "User"}
        base.comment_provenance(witness, edited=True)
        base.seal(snapshot, policy)
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])
        for prefix, suffix in [("```\n", "\n```"), ("> ", ""), ("Explanation\n", "")]:
            snapshot, policy, witness = native_fixture()
            body = witness["body"].removesuffix(FOOTER)
            witness["body"] = prefix + body + suffix + FOOTER
            base.comment_provenance(witness, edited=True)
            base.seal(snapshot, policy)
            self.assertFalse(gate.evaluate(snapshot, policy)["allow"])

    def test_native_review_and_checks_remain_required(self):
        snapshot, policy, _ = native_fixture()
        snapshot["reviews"] = [
            r for r in snapshot["reviews"] if r["user"] != base.CODEX
        ]
        snapshot["review_meta"] = [
            r for r in snapshot["review_meta"] if r["databaseId"] != 30
        ]
        base.seal(snapshot, policy)
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])
        snapshot, policy, _ = native_fixture()
        snapshot["checks"][0]["conclusion"] = "failure"
        base.seal(snapshot, policy)
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])


if __name__ == "__main__":
    unittest.main()
