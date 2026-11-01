"""Keep native review and independent handling attestations on separate identities."""

import json
import unittest
from pathlib import Path

ROOT = next(
    p
    for p in Path(__file__).resolve().parents
    if (p / ".github/maintenance/merge-policy.json").is_file()
)


class ReviewerIdentityTests(unittest.TestCase):
    def test_independent_witness_is_neither_native_reviewer_nor_ledger_author(
        self,
    ) -> None:
        policy = json.loads(
            (ROOT / ".github/maintenance/merge-policy.json").read_bytes()
        )
        native = {actor["id"] for actor in policy["codex_reviewers"]}
        authors = {actor["id"] for actor in policy["trusted_ledger_authors"]}
        independent = {actor["id"] for actor in policy["independent_reviewers"]}
        self.assertTrue(native)
        self.assertTrue(independent.isdisjoint(native | authors))
        self.assertEqual(
            policy["independent_reviewers"],
            [{"id": 351004977, "login": "barneyonline-hal-reviewer[bot]"}],
        )


if __name__ == "__main__":
    unittest.main()
