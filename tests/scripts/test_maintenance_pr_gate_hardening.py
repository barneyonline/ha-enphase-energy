"""Adversarial gate regressions and real API-shape/transport contracts."""

from __future__ import annotations

import copy
import io
import json
import runpy
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

try:
    import test_maintenance_pr_gate as fixtures
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).parent))
    import test_maintenance_pr_gate as fixtures

gate = fixtures.gate
HEAD, BASE, WHEN, LATER = fixtures.HEAD, fixtures.BASE, fixtures.WHEN, fixtures.LATER
REPOSITORY, AUTHOR, CODEX, INDEPENDENT, URL = (
    fixtures.REPOSITORY,
    fixtures.AUTHOR,
    fixtures.CODEX,
    fixtures.INDEPENDENT,
    fixtures.URL,
)


class HardeningTests(unittest.TestCase):
    def test_older_workflow_record_cannot_replace_a_newer_success(self):
        snapshot, policy = fixtures.fixture()
        old = copy.deepcopy(snapshot["workflows"][0])
        old.update(
            id=19, run_number=4, status="completed", conclusion="failure", _jobs=[]
        )
        snapshot["workflows"].append(old)
        verdict = gate.evaluate(snapshot, policy)
        self.assertTrue(verdict["allow"], verdict)

    def test_addressed_cannot_launder_resolved_substantive_finding(self):
        snapshot, policy = fixtures.fixture()
        fixtures.drop_approval(snapshot)
        finding = "[P1] Restore the safety interlock before merging."
        snapshot["reviews"][0]["body"] = finding
        snapshot["review_meta"][0]["body"] = finding
        snapshot["review_comments"].append(
            {
                "id": 40,
                "user": CODEX,
                "body": finding,
                "updated_at": WHEN,
                "html_url": "https://github.com/example/integration/pull/1#discussion_r40",
            }
        )
        snapshot["threads"].append(
            {
                "id": "thread40",
                "isResolved": True,
                "isOutdated": False,
                "comments": [{"databaseId": 40, "body": finding, "updatedAt": WHEN}],
            }
        )
        fixtures.seal(
            snapshot,
            policy,
            {
                key: {
                    "disposition": "addressed",
                    "reason": "False positive; no change.",
                }
                for key in ("review:30", "inline_comment:40")
            },
        )
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            fixtures.codes(snapshot, policy),
        )

    def test_old_ledger_like_comments_with_appended_findings_are_feedback(self):
        for body in (
            "<!-- hal-feedback-ledger:v1 {} -->\n[P1] Do not merge.",
            '<!-- hal-feedback-ledger:v1 {"schema":99,"finding":"Regression"} -->',
            "A quoted <!-- hal-feedback-ledger:v1 {} --> is not a ledger.",
        ):
            snapshot, policy = fixtures.fixture()
            snapshot["issue_comments"].insert(
                0,
                {
                    "id": 900,
                    "user": AUTHOR,
                    "body": body,
                    "updated_at": WHEN,
                    "html_url": "https://github.com/example/integration/pull/1#issuecomment-900",
                },
            )
            result = gate.evaluate(snapshot, policy)
            self.assertFalse(result["allow"])
            self.assertIn(
                "issue_comment:900", {f["source"] for f in result["feedback"]}
            )

    def test_codex_cannot_be_pr_or_ledger_author_even_with_changed_login_case(self):
        for role in ("pr", "ledger"):
            snapshot, policy = fixtures.fixture()
            if role == "pr":
                snapshot["pr"]["user"] = {
                    "id": CODEX["id"],
                    "login": CODEX["login"].upper(),
                }
            else:
                policy["trusted_ledger_authors"].append(CODEX)
            fixtures.seal(snapshot, policy)
            if role == "ledger":
                snapshot["issue_comments"][-1]["user"] = CODEX
            self.assertIn(
                "codex_review_missing_or_stale", fixtures.codes(snapshot, policy)
            )

    def test_final_pr_metadata_changes_detected_without_head_movement(self):
        for field, value in (
            ("draft", True),
            ("state", "closed"),
            ("merged", True),
            ("mergeable", False),
            ("mergeable_state", "dirty"),
            ("base", {"sha": BASE, "ref": "other"}),
        ):
            snapshot, policy = fixtures.fixture()
            ordinary = fixtures.ClientTests().full_transport(snapshot, policy)
            reads = 0

            def transport(url, query, ordinary=ordinary, field=field, value=value):
                nonlocal reads
                data, headers = ordinary(url, query)
                if url.endswith("/pulls/1"):
                    reads += 1
                    if reads == 5:
                        data[field] = value
                return data, headers

            with (
                self.subTest(field=field),
                self.assertRaisesRegex(gate.GateError, "PR state"),
            ):
                gate.GitHub(REPOSITORY, "placeholder", transport).collect(1)

    def test_base_bound_ci_required(self):
        for links in (
            [{"number": 1, "head": {"sha": HEAD}, "base": {"sha": "f" * 40}}],
        ):
            snapshot, policy = fixtures.fixture()
            snapshot["workflows"][0]["pull_requests"] = links
            fixtures.seal(snapshot, policy)
            reasons = fixtures.codes(snapshot, policy)
            self.assertIn("workflow_current_base_unproven", reasons)

    def test_trusted_compare_proves_current_base_when_real_run_metadata_is_empty(self):
        snapshot, policy = fixtures.fixture()
        snapshot["workflows"][0]["pull_requests"] = []
        fixtures.seal(snapshot, policy)
        collected, actual = gate.GitHub(
            REPOSITORY,
            "fixture",
            fixtures.ClientTests().full_transport(snapshot, policy),
        ).collect(1)
        self.assertTrue(gate.evaluate(collected, actual)["allow"])
        for change in (
            {"merge_base_commit": {"sha": "f" * 40}},
            {"behind_by": 1},
            {"status": "diverged"},
            {"_requested_base_sha": "f" * 40},
            {"_requested_head_sha": "f" * 40},
            {"base_commit": {"sha": "f" * 40}},
        ):
            invalid = copy.deepcopy(snapshot)
            invalid["comparison"].update(change)
            self.assertIn(
                "current_base_ancestry_unproven", fixtures.codes(invalid, policy)
            )
        snapshot["pr"]["base"]["sha"] = "f" * 40
        fixtures.seal(snapshot, policy)
        self.assertIn(
            "current_base_ancestry_unproven", fixtures.codes(snapshot, policy)
        )

    def test_jobs_optional_attempt_comes_from_specific_endpoint(self):
        snapshot, policy = fixtures.fixture()
        del snapshot["workflows"][0]["_jobs"][0]["run_attempt"]
        result, actual = gate.GitHub(
            REPOSITORY,
            "placeholder",
            fixtures.ClientTests().full_transport(snapshot, policy),
        ).collect(1)
        self.assertEqual(result["checks"][0]["_workflow"]["run_attempt"], 1)
        self.assertTrue(gate.evaluate(result, actual)["allow"])
        snapshot["workflows"][0]["_jobs"][0]["run_attempt"] = 2
        with self.assertRaisesRegex(gate.GateError, "attempt"):
            gate.GitHub(
                REPOSITORY,
                "placeholder",
                fixtures.ClientTests().full_transport(snapshot, policy),
            ).collect(1)

    def test_ref_qualified_and_exact_synthetic_codeql_paths(self):
        for path in (
            ".github/workflows/tests.yml@main",
            "dynamic/github-code-scanning/codeql",
        ):
            snapshot, policy = fixtures.fixture()
            if path.startswith("dynamic"):
                policy["checks"][0]["workflow_path"] = path
            snapshot["workflows"][0]["path"] = path
            snapshot["checks"][0]["_workflow"]["path"] = path
            fixtures.seal(snapshot, policy)
            fixtures.attest(snapshot, policy)
            self.assertTrue(
                gate.evaluate(snapshot, policy)["allow"],
                gate.evaluate(snapshot, policy),
            )
        snapshot["workflows"][0]["path"] = "dynamic/unknown-scanner"
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])

    def test_check_suite_cap_and_missing_suite_block(self):
        snapshot, policy = fixtures.fixture()
        snapshot["check_suites"] = [{"id": n} for n in range(1, 1001)]
        with self.assertRaisesRegex(gate.GateError, "API limit"):
            gate.GitHub(
                REPOSITORY,
                "placeholder",
                fixtures.ClientTests().full_transport(snapshot, policy),
            ).collect(1)
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])
        snapshot["check_suites"] = []
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])

    def test_structured_verdict_binds_dispositions(self):
        snapshot, policy = fixtures.fixture()
        fixtures.drop_approval(snapshot)
        verdict = {
            "schema": 1,
            "repository": REPOSITORY,
            "pull_number": 1,
            "head_sha": HEAD,
            "base_sha": BASE,
            "policy_sha256": gate.digest(policy),
            "feedback_digest": "0" * 64,
            "dispositions_digest": "0" * 64,
            "verdict": "clean",
            "reason": "Independent handling verified.",
            "evidence_urls": [URL],
        }
        witness = {
            "id": 90,
            "user": INDEPENDENT,
            "updated_at": LATER,
            "html_url": "https://github.com/example/integration/pull/1#issuecomment-90",
            "body": "<!-- " + gate.CLEAN_MARKER + " " + json.dumps(verdict) + " -->",
        }
        snapshot["issue_comments"].append(witness)
        fixtures.comment_provenance(witness)
        fixtures.seal(snapshot, policy)
        report = gate.evaluate(snapshot, policy)
        self.assertFalse(report["allow"])
        verdict.update(
            {
                key: value
                for key, value in report["handling_candidates"][0].items()
                if key != "source"
            }
        )
        witness["body"] = (
            "<!-- " + gate.CLEAN_MARKER + " " + json.dumps(verdict) + " -->"
        )
        fixtures.comment_provenance(witness, edited=True)
        fixtures.seal(snapshot, policy)
        self.assertTrue(
            gate.evaluate(snapshot, policy)["allow"], gate.evaluate(snapshot, policy)
        )
        fixtures.seal(
            snapshot,
            policy,
            {
                "issue_comment:10": {
                    "disposition": "false-positive",
                    "reason": "Changed after verdict.",
                }
            },
        )
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            fixtures.codes(snapshot, policy),
        )

    def test_new_feedback_after_approval_requires_new_independent_handling(self):
        snapshot, policy = fixtures.fixture()
        snapshot["issue_comments"][0]["updated_at"] = "2026-11-01T01:03:00Z"
        fixtures.seal(snapshot, policy)
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            fixtures.codes(snapshot, policy),
        )

    def test_malformed_policy_and_sources_fail_closed(self):
        mutations = [
            lambda s, p: p.update(schema=2),
            lambda s, p: p.update(repository="bad"),
            lambda s, p: p.update(codex_reviewers=[]),
            lambda s, p: p["codex_reviewers"].append(CODEX),
            lambda s, p: p.update(checks=None),
            lambda s, p: p["checks"].append(copy.deepcopy(p["checks"][0])),
            lambda s, p: p["checks"][0].update(required=False),
            lambda s, p: p["checks"][0].update(required="yes"),
            lambda s, p: p["statuses"][0].update(required="yes"),
            lambda s, p: p["statuses"].append(copy.deepcopy(p["statuses"][0])),
            lambda s, p: p["checks"][0].update(
                workflow_path=".github/workflows/tests.yml@"
            ),
            lambda s, p: p["checks"][0].update(workflow_path="dynamic/other"),
            lambda s, p: s.update(schema=2),
            lambda s, p: s.update(errors=["partial collection"]),
            lambda s, p: s.update(checks=None),
            lambda s, p: s["pr"]["head"].update(sha="bad"),
            lambda s, p: s.update(repository="other/repo"),
            lambda s, p: s["pr"].update(draft=True),
            lambda s, p: s["reviews"][0]["user"].update(id=0),
            lambda s, p: s["issue_comments"][0].update(body=None),
            lambda s, p: s["issue_comments"][0].update(
                updated_at="2026-11-01T00:00:00"
            ),
            lambda s, p: s["review_meta"].append(copy.deepcopy(s["review_meta"][0])),
            lambda s, p: s["reviews"][0].update(state="UNKNOWN"),
            lambda s, p: s["reviews"][0].update(commit_id=None),
            lambda s, p: s["review_meta"][0].update(body="changed independently"),
            lambda s, p: s["review_meta"].append({"databaseId": 99}),
            lambda s, p: s["checks"][0]["output"].update(annotations_count=1),
            lambda s, p: s["issue_comments"].append(
                copy.deepcopy(s["issue_comments"][0])
            ),
            lambda s, p: s["statuses"][0].update(context="unknown"),
            lambda s, p: s["workflows"][0].update(head_sha="c" * 40),
            lambda s, p: s["workflows"][0].pop("_jobs"),
            lambda s, p: s["workflows"][0].update(path=".github/workflows/unknown.yml"),
            lambda s, p: s.update(workflows=[]),
        ]
        for index, mutate in enumerate(mutations):
            snapshot, policy = fixtures.fixture()
            mutate(snapshot, policy)
            with self.subTest(index=index):
                self.assertFalse(gate.evaluate(snapshot, policy)["allow"])

    def test_ledger_schema_and_evidence_contracts(self):
        mutations = [
            lambda ledger: ledger.update(schema=99),
            lambda ledger: ledger.update(head_sha="bad"),
            lambda ledger: ledger.update(entries="bad"),
            lambda ledger: ledger["entries"][0].update(extra="finding"),
            lambda ledger: ledger["entries"][0].update(reason=9),
            lambda ledger: ledger["entries"][0].update(evidence_urls=[9]),
        ]
        for mutation in mutations:
            snapshot, policy = fixtures.fixture()
            comment = snapshot["issue_comments"][-1]
            ledger = gate.marker(comment["body"], gate.LEDGER_MARKER)
            mutation(ledger)
            comment["body"] = (
                "<!-- " + gate.LEDGER_MARKER + " " + json.dumps(ledger) + " -->"
            )
            self.assertFalse(gate.evaluate(snapshot, policy)["allow"])
        for override in (
            {"reason": " "},
            {"evidence_urls": []},
            {"evidence_urls": ["https://github.com/example/integration/unknown/1"]},
        ):
            snapshot, policy = fixtures.fixture()
            fixtures.seal(snapshot, policy, {"issue_comment:10": override})
            self.assertFalse(gate.evaluate(snapshot, policy)["allow"])
        snapshot, policy = fixtures.fixture()
        comment = snapshot["issue_comments"][-1]
        ledger = gate.marker(comment["body"], gate.LEDGER_MARKER)
        ledger["entries"].append(copy.deepcopy(ledger["entries"][0]))
        comment["body"] = (
            "<!-- " + gate.LEDGER_MARKER + " " + json.dumps(ledger) + " -->"
        )
        self.assertIn("invalid_evidence", fixtures.codes(snapshot, policy))

    def test_optional_context_required_typo_does_not_hide_behind_valid_required_check(
        self,
    ):
        snapshot, policy = fixtures.fixture()
        policy["checks"].append(
            {
                "app_id": 15368,
                "name": "optional",
                "workflow_path": ".github/workflows/optional.yml",
                "required": "false",
            }
        )
        self.assertIn("invalid_evidence", fixtures.codes(snapshot, policy))


class DigestAcceptanceTests(unittest.TestCase):
    @staticmethod
    def append_clean(snapshot, policy, ident):
        # The prospective digest covers every existing verdict. Only the newly
        # emitted candidate will be excluded when the gate verifies it.
        report = gate.evaluate(snapshot, policy)
        verdict = {
            "schema": 1,
            "repository": REPOSITORY,
            "pull_number": 1,
            "head_sha": HEAD,
            "base_sha": BASE,
            "policy_sha256": gate.digest(policy),
            **report["handling_digests"],
            "verdict": "clean",
            "reason": "Independent reviewer rechecked all current sources and dispositions.",
            "evidence_urls": [URL],
        }
        snapshot["issue_comments"].append(
            {
                "id": ident,
                "user": INDEPENDENT,
                "updated_at": LATER,
                "html_url": f"https://github.com/example/integration/pull/1#issuecomment-{ident}",
                "body": "<!-- "
                + gate.CLEAN_MARKER
                + " "
                + json.dumps(verdict)
                + " -->",
            }
        )
        fixtures.comment_provenance(snapshot["issue_comments"][-1])
        fixtures.seal(snapshot, policy)

    def test_bare_approved_review_never_substitutes_for_digest_binding(self):
        snapshot, policy = fixtures.fixture()
        fixtures.drop_handling_witness(snapshot)
        snapshot["reviews"][1]["body"] = "Approved; all looks good."
        snapshot["review_meta"][1]["body"] = "Approved; all looks good."
        fixtures.seal(snapshot, policy)
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            fixtures.codes(snapshot, policy),
        )

    def test_changed_formal_review_invalidates_issue_comment_handling_authority(self):
        for changes in ({"state": "DISMISSED"}, {"commit_id": "c" * 40}):
            snapshot, policy = fixtures.fixture()
            snapshot["reviews"][1].update(changes)
            snapshot["review_meta"][1]["state"] = snapshot["reviews"][1]["state"]
            snapshot["review_meta"][1]["commit"]["oid"] = snapshot["reviews"][1][
                "commit_id"
            ]
            fixtures.seal(snapshot, policy)
            self.assertIn(
                "independent_handling_verdict_missing_or_stale",
                fixtures.codes(snapshot, policy),
            )

    def test_completed_check_output_or_annotations_can_change_without_timestamp(self):
        for change in ("summary", "annotations"):
            snapshot, policy = fixtures.fixture()
            completed = snapshot["checks"][0]["completed_at"]
            if change == "summary":
                snapshot["checks"][0]["output"][
                    "summary"
                ] = "[P1] New safety interlock regression."
            else:
                snapshot["checks"][0]["output"]["annotations_count"] = 1
                snapshot["checks"][0]["_annotations"] = [
                    {"message": "[P1] New interlock regression."}
                ]
            fixtures.seal(snapshot, policy)
            self.assertEqual(snapshot["checks"][0]["completed_at"], completed)
            self.assertIn(
                "independent_handling_verdict_missing_or_stale",
                fixtures.codes(snapshot, policy),
            )
            fixtures.attest(snapshot, policy)
            self.assertTrue(gate.evaluate(snapshot, policy)["allow"])

    def test_author_cannot_change_dispositions_after_independent_verdict(self):
        snapshot, policy = fixtures.fixture()
        fixtures.seal(
            snapshot,
            policy,
            {
                "issue_comment:10": {
                    "disposition": "false-positive",
                    "reason": "Author dismisses this now.",
                }
            },
        )
        self.assertIn(
            "independent_handling_verdict_missing_or_stale",
            fixtures.codes(snapshot, policy),
        )

    def test_adverse_invalid_and_other_scope_verdicts_remain_covered_feedback(self):
        for change in (
            {"verdict": "blocked", "reason": "[P1] New finding: do not merge."},
            {"repository": "other/repo"},
            {"schema": 99},
            {"schema": True},
            {"pull_number": True},
            {"reason": ""},
            {"evidence_urls": []},
            {"feedback_digest": "not-a-digest"},
            {"extra": "[P1] Unhandled finding"},
        ):
            snapshot, policy = fixtures.fixture()
            witness = next(
                comment
                for comment in snapshot["issue_comments"]
                if comment["id"] == fixtures.WITNESS_ID
            )
            verdict = gate.marker(witness["body"], gate.CLEAN_MARKER)
            verdict.update(change)
            snapshot["issue_comments"].append(
                {
                    "id": 91,
                    "user": INDEPENDENT,
                    "updated_at": LATER,
                    "html_url": "https://github.com/example/integration/pull/1#issuecomment-91",
                    "body": "<!-- "
                    + gate.CLEAN_MARKER
                    + " "
                    + json.dumps(verdict)
                    + " -->",
                }
            )
            fixtures.seal(snapshot, policy)
            result = gate.evaluate(snapshot, policy)
            self.assertFalse(result["allow"], result)
            self.assertIn(
                "issue_comment:91", {item["source"] for item in result["feedback"]}
            )
            self.assertNotIn(
                "issue_comment:91",
                {item["source"] for item in result["handling_candidates"]},
            )

    def test_sequential_clean_witnesses_bind_previous_witnesses_without_self_hash_loop(
        self,
    ):
        snapshot, policy = fixtures.fixture()
        self.assertEqual(
            gate.evaluate(snapshot, policy)["handling_evidence"]["source"],
            fixtures.WITNESS_SOURCE,
        )
        for ident in (91, 92):
            self.append_clean(snapshot, policy, ident)
            result = gate.evaluate(snapshot, policy)
            self.assertTrue(result["allow"], result)
            self.assertEqual(
                result["handling_evidence"]["source"], f"issue_comment:{ident}"
            )
        witness = next(
            comment
            for comment in snapshot["issue_comments"]
            if comment["id"] == fixtures.WITNESS_ID
        )
        prior = gate.marker(witness["body"], gate.CLEAN_MARKER)
        prior["reason"] = "Changed older verdict after both later verdict comments."
        body = "<!-- " + gate.CLEAN_MARKER + " " + json.dumps(prior) + " -->"
        witness["body"] = body
        fixtures.comment_provenance(witness, edited=True)
        fixtures.seal(snapshot, policy)
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])
        self.append_clean(snapshot, policy, 93)
        self.assertTrue(gate.evaluate(snapshot, policy)["allow"])


class ProtocolAndBoundaryTests(unittest.TestCase):
    def test_transport_json_get_query_and_size_limits(self):
        client = gate.GitHub(REPOSITORY, "fixture-token")
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.read.return_value = b'{"ok":true}'
        client.opener = mock.Mock()
        client.opener.open.return_value = response
        self.assertEqual(client.get("pulls/1"), {"ok": True})
        self.assertEqual(
            client.request("/graphql", {"query": "query($id:ID!){node(id:$id){id}}"})[
                0
            ],
            {"ok": True},
        )
        response.headers = {"Content-Length": "20000001"}
        with self.assertRaisesRegex(gate.GateError, "too large"):
            client.get("pulls/1")
        response.headers = {}
        response.read.return_value = b"x" * 20_000_001
        with self.assertRaisesRegex(gate.GateError, "too large"):
            client.get("pulls/1")

    def test_network_errors_do_not_echo_credentials_or_response_body(self):
        client = gate.GitHub(REPOSITORY, "fixture-token")
        client.opener = mock.Mock()
        errors = [
            urllib.error.HTTPError(
                "https://api.github.com/x",
                403,
                "secret response",
                {},
                io.BytesIO(b"hidden"),
            ),
            urllib.error.URLError("hidden detail"),
            TimeoutError("hidden detail"),
        ]
        for error in errors:
            client.opener.open.side_effect = error
            with self.assertRaises(gate.GateError) as failure:
                client.get("pulls/1")
            self.assertNotIn("hidden", str(failure.exception))
            self.assertNotIn("fixture-token", str(failure.exception))
            if isinstance(error, urllib.error.HTTPError):
                error.close()
        with self.assertRaises(gate.GateError):
            gate.NoRedirect().redirect_request(
                None, None, 302, "redirect", {}, "https://evil.test"
            )
        with self.assertRaises(gate.GateError):
            gate.GitHub(REPOSITORY, "")

    def test_pagination_malformed_collections_and_hard_limits(self):
        for response in (({}, {}), ([], {"Link": 'malformed; rel="next"'})):
            with self.assertRaises(gate.GateError):
                gate.GitHub(
                    REPOSITORY, "fixture", lambda u, q, response=response: response
                ).pages("pulls/1/reviews")
        count = 0

        def endless(url, query):
            nonlocal count
            count += 1
            return [], {
                "Link": f'<https://api.github.com/repos/{REPOSITORY}/pulls/1/reviews?page={count}>; rel="next"'
            }

        with self.assertRaisesRegex(gate.GateError, "pagination limit"):
            gate.GitHub(REPOSITORY, "fixture", endless).pages("pulls/1/reviews")

    def test_graphql_malformed_pagination_loop_and_hard_limit(self):
        for payload in (
            {"data": {}},
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviews": {"nodes": {}, "pageInfo": {"hasNextPage": False}}
                        }
                    }
                }
            },
        ):
            with self.assertRaises(gate.GateError):
                gate.GitHub(
                    REPOSITORY, "fixture", lambda u, q, payload=payload: (payload, {})
                ).graphql_pages(1, "reviews", "databaseId")
        count = 0

        def response(repeat):
            def transport(url, query):
                nonlocal count
                count += 1
                return {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviews": {
                                    "nodes": [],
                                    "pageInfo": {
                                        "hasNextPage": True,
                                        "endCursor": "repeat" if repeat else str(count),
                                    },
                                }
                            }
                        }
                    }
                }, {}

            return transport

        for repeat, expected in ((True, "loop"), (False, "limit")):
            with self.assertRaisesRegex(gate.GateError, expected):
                gate.GitHub(REPOSITORY, "fixture", response(repeat)).graphql_pages(
                    1, "reviews", "databaseId"
                )

    def test_collector_thread_pagination_annotations_and_transport_contract_errors(
        self,
    ):
        helper = fixtures.GateTests()
        helper.setUp()
        helper.add_thread(resolved=True)
        snapshot, policy = helper.snapshot, helper.policy
        fixtures.attest(snapshot, policy)
        normal = fixtures.ClientTests().full_transport(snapshot, policy)

        def transport(url, query):
            if query and "node(id:$id)" in query["query"]:
                return {
                    "data": {
                        "node": {
                            "comments": {
                                "nodes": snapshot["threads"][0]["comments"],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }, {}
            data, headers = normal(url, query)
            if query and "reviewThreads(first:" in query["query"]:
                data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0][
                    "comments"
                ]["pageInfo"]["hasNextPage"] = True
            return data, headers

        collected, actual = gate.GitHub(REPOSITORY, "fixture", transport).collect(1)
        self.assertTrue(gate.evaluate(collected, actual)["allow"])
        for mutation, expected in (
            (lambda d: d["checks"][0].update(id=999), "matching workflow"),
            (
                lambda d: d["checks"][0]["output"].update(annotations_count=None),
                "annotation count",
            ),
        ):
            snap = copy.deepcopy(snapshot)
            mutation(snap)
            with self.assertRaisesRegex(gate.GateError, expected):
                gate.GitHub(
                    REPOSITORY,
                    "fixture",
                    fixtures.ClientTests().full_transport(snap, policy),
                ).collect(1)
        snap = copy.deepcopy(snapshot)
        snap["checks"][0]["output"]["annotations_count"] = 1
        normal = fixtures.ClientTests().full_transport(snap, policy)

        def annotations(url, query):
            if "/annotations?" in url:
                return [], {}
            return normal(url, query)

        with self.assertRaisesRegex(gate.GateError, "incomplete check annotations"):
            gate.GitHub(REPOSITORY, "fixture", annotations).collect(1)

    def test_collector_policy_and_base_races_fail_closed(self):
        snapshot, policy = fixtures.fixture()
        normal = fixtures.ClientTests().full_transport(snapshot, policy)

        def bad_policy(url, query):
            if "/contents/" in url:
                return {"type": "file", "encoding": "utf-8", "content": "{}"}, {}
            return normal(url, query)

        with self.assertRaisesRegex(gate.GateError, "trusted base policy"):
            gate.GitHub(REPOSITORY, "fixture", bad_policy).collect(1)
        reads = 0

        def base_move(url, query):
            nonlocal reads
            data, headers = normal(url, query)
            if url.endswith("/pulls/1"):
                reads += 1
                if reads >= 2:
                    data["base"]["sha"] = "f" * 40
            return data, headers

        with self.assertRaisesRegex(gate.GateError, "base changed after"):
            gate.GitHub(REPOSITORY, "fixture", base_move).collect(1)

    def test_additional_review_thread_and_ledger_boundary_contracts(self):
        helper = fixtures.GateTests()
        helper.setUp()
        helper.add_thread(resolved=True)
        for mutation in (
            lambda s: s["threads"][0].update(isResolved="true"),
            lambda s: s["threads"][0].update(comments=[]),
            lambda s: s["threads"][0]["comments"].append(
                copy.deepcopy(s["threads"][0]["comments"][0])
            ),
            lambda s: s.update(threads=[]),
        ):
            snap = copy.deepcopy(helper.snapshot)
            mutation(snap)
            self.assertIn("invalid_evidence", fixtures.codes(snap, helper.policy))
        snapshot, policy = fixtures.fixture()
        for changes in ({"repository": "other/repo"}, {"policy_sha256": "0" * 64}):
            snap = copy.deepcopy(snapshot)
            comment = snap["issue_comments"][-1]
            ledger = gate.marker(comment["body"], gate.LEDGER_MARKER)
            ledger.update(changes)
            comment["body"] = (
                "<!-- " + gate.LEDGER_MARKER + " " + json.dumps(ledger) + " -->"
            )
            self.assertFalse(gate.evaluate(snap, policy)["allow"])
        for urls in (None, [9]):
            self.assertFalse(gate.evidence_urls(urls, REPOSITORY))

    def test_external_check_and_native_gate_run_have_precise_identities(self):
        snapshot, policy = fixtures.fixture()
        external = copy.deepcopy(snapshot["checks"][0])
        external.update(
            id=888,
            name="CodeQL",
            app={"id": 57789, "slug": "github-advanced-security"},
            _workflow=None,
        )
        snapshot["checks"].append(external)
        policy["checks"].append(
            {"app_id": 57789, "name": "CodeQL", "workflow_path": None, "required": True}
        )
        native = copy.deepcopy(snapshot["workflows"][0])
        native_check = copy.deepcopy(snapshot["checks"][0])
        native_check.update(id=889, name=policy["operational_check"]["name"])
        native_check["_workflow"].update(
            id=88, path=policy["operational_check"]["workflow_path"]
        )
        snapshot["checks"].append(native_check)
        native.update(
            id=88,
            path=policy["operational_check"]["workflow_path"],
            status="in_progress",
            conclusion=None,
            _jobs=[
                {
                    "name": policy["operational_check"]["name"],
                    "run_attempt": 1,
                    "check_run_url": "https://api.github.com/repos/example/integration/check-runs/889",
                }
            ],
        )
        snapshot["workflows"].append(native)
        fixtures.seal(snapshot, policy)
        fixtures.attest(snapshot, policy)
        self.assertTrue(gate.evaluate(snapshot, policy)["allow"])
        snapshot["checks"] = [c for c in snapshot["checks"] if c["name"] != "pytest"]
        fixtures.seal(snapshot, policy)
        self.assertIn("required_check_missing", fixtures.codes(snapshot, policy))

    def test_cli_live_and_errors_never_claim_offline_authority(self):
        snapshot, policy = fixtures.fixture()
        fake = mock.Mock()
        fake.collect.return_value = (snapshot, policy)
        with (
            mock.patch.object(gate, "GitHub", return_value=fake),
            mock.patch.dict(gate.os.environ, {"GH_TOKEN": "fixture"}),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(
                gate.main(["--repository", REPOSITORY, "--pull-number", "1"]), 0
            )
            self.assertTrue(json.loads(output.getvalue())["merge_authority"])
        for args in (
            ["--snapshot", "missing.json"],
            [],
            [
                "--policy",
                "local.json",
                "--repository",
                REPOSITORY,
                "--pull-number",
                "1",
            ],
            ["--snapshot", "missing.json", "--policy", "missing-policy.json"],
        ):
            with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(gate.main(args), 2)
                self.assertFalse(json.loads(output.getvalue())["merge_authority"])
        with (
            mock.patch.object(gate, "GitHub", side_effect=TypeError("do not echo me")),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(
                gate.main(["--repository", REPOSITORY, "--pull-number", "1"]), 2
            )
            self.assertNotIn("do not echo me", output.getvalue())

    def test_executable_main_entrypoint_is_offline_only_with_fixture_input(self):
        import tempfile

        snapshot, policy = fixtures.fixture()
        with tempfile.TemporaryDirectory() as folder:
            snap_path, policy_path = (
                Path(folder) / "snapshot.json",
                Path(folder) / "policy.json",
            )
            snap_path.write_text(json.dumps(snapshot))
            policy_path.write_text(json.dumps(policy))
            argv = [
                "pr_gate.py",
                "--snapshot",
                str(snap_path),
                "--policy",
                str(policy_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch("sys.stdout", new_callable=io.StringIO) as output,
                self.assertRaises(SystemExit) as status,
            ):
                runpy.run_path(gate.__file__, run_name="__main__")
            self.assertEqual(status.exception.code, 0)
            self.assertFalse(json.loads(output.getvalue())["merge_authority"])


class OperationalIdentityTests(unittest.TestCase):
    @staticmethod
    def snapshot():
        snapshot, policy = fixtures.fixture()
        policy["gate_check"] = {
            "name": "Autonomous maintenance / merge gate",
            "app_id": 15368,
            "workflow_path": None,
        }
        fixtures.seal(snapshot, policy)
        fixtures.attest(snapshot, policy)
        original = snapshot["checks"][0]
        custom, native = copy.deepcopy(original), copy.deepcopy(original)
        custom.update(
            id=301,
            name=policy["gate_check"]["name"],
            _workflow=None,
            status="in_progress",
            conclusion=None,
            completed_at=None,
            check_suite={"id": 12},
        )
        native.update(
            id=302,
            name="maintenance-gate",
            status="in_progress",
            conclusion=None,
            completed_at=None,
            check_suite={"id": 12},
        )
        native["_workflow"].update(id=88, path=".github/workflows/maintenance-gate.yml")
        snapshot["checks"].extend([custom, native])
        snapshot["check_suites"].append(
            {"id": 12, "status": "in_progress", "conclusion": None}
        )
        run = copy.deepcopy(snapshot["workflows"][0])
        run.update(
            id=88,
            path=".github/workflows/maintenance-gate.yml",
            event="pull_request_target",
            status="in_progress",
            conclusion=None,
            pull_requests=[],
            _jobs=[
                {
                    "id": 302,
                    "name": "maintenance-gate",
                    "run_attempt": 1,
                    "check_run_url": "https://api.github.com/repos/example/integration/check-runs/302",
                }
            ],
        )
        snapshot["workflows"].append(run)
        return snapshot, policy

    def test_exact_native_custom_and_self_only_suite_changes_do_not_invalidate_decision(
        self,
    ):
        snapshot, policy = self.snapshot()
        first = gate.evaluate(snapshot, policy)
        self.assertTrue(first["allow"], first)
        self.assertNotIn("check:301", {item["source"] for item in first["feedback"]})
        self.assertNotIn("check:302", {item["source"] for item in first["feedback"]})
        for check in snapshot["checks"][1:]:
            check.update(status="completed", conclusion="success", completed_at=LATER)
        snapshot["check_suites"][-1].update(status="completed", conclusion="success")
        snapshot["workflows"][-1].update(status="completed", conclusion="success")
        final = gate.evaluate(snapshot, policy)
        self.assertTrue(final["allow"], final)
        for key in ("inventory_digest", "snapshot_digest", "handling_digests"):
            self.assertEqual(first[key], final[key])

    def test_unexpected_job_or_check_in_self_suite_is_never_ignored(self):
        snapshot, policy = self.snapshot()
        extra = copy.deepcopy(snapshot["checks"][-1])
        extra.update(id=303, name="unexpected-job")
        snapshot["checks"].append(extra)
        snapshot["workflows"][-1]["_jobs"].append(
            {
                "id": 303,
                "name": "unexpected-job",
                "run_attempt": 1,
                "check_run_url": "https://api.github.com/repos/example/integration/check-runs/303",
            }
        )
        fixtures.seal(snapshot, policy)
        fixtures.attest(snapshot, policy)
        result = gate.evaluate(snapshot, policy)
        self.assertFalse(result["allow"])
        self.assertIn("check:303", {item["source"] for item in result["feedback"]})
        self.assertIn(
            "unknown_check_context", {item["code"] for item in result["reasons"]}
        )
        self.assertIn(
            "latest_workflow_not_successful",
            {item["code"] for item in result["reasons"]},
        )
        self.assertIn(
            12,
            {
                suite["id"]
                for suite in gate.decision_view(snapshot, policy)["check_suites"]
            },
        )
        snapshot, policy = self.snapshot()
        snapshot["workflows"][-1]["_jobs"].append(
            {"name": "unexpected-job-without-check-yet"}
        )
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])
        snapshot, policy = self.snapshot()
        snapshot["check_suites"].append(
            {"id": 99, "status": "pending", "conclusion": None}
        )
        self.assertFalse(gate.evaluate(snapshot, policy)["allow"])

    def test_duplicate_same_named_native_job_or_check_is_covered_and_blocks(self):
        for with_job in (True, False):
            snapshot, policy = self.snapshot()
            extra = copy.deepcopy(snapshot["checks"][-1])
            extra.update(
                id=303,
                status="completed",
                conclusion="failure",
                completed_at=LATER,
                output={
                    "title": "Validation",
                    "summary": "[P1] Additional validation failed.",
                    "annotations_count": 0,
                },
            )
            snapshot["checks"].append(extra)
            if with_job:
                snapshot["workflows"][-1]["_jobs"].append(
                    {
                        "id": 303,
                        "name": "maintenance-gate",
                        "run_attempt": 1,
                        "check_run_url": "https://api.github.com/repos/example/integration/check-runs/303",
                    }
                )
            snapshot["workflows"][-1].update(status="completed", conclusion="failure")
            fixtures.seal(snapshot, policy)
            fixtures.attest(snapshot, policy)
            result = gate.evaluate(snapshot, policy)
            self.assertFalse(result["allow"], result)
            self.assertIn("check:303", {item["source"] for item in result["feedback"]})
            view = gate.decision_view(snapshot, policy)
            self.assertIn(303, {check["id"] for check in view["checks"]})
            self.assertIn(88, {run["id"] for run in view["workflows"]})
            self.assertIn(12, {suite["id"] for suite in view["check_suites"]})

    def test_historical_native_attempts_each_have_one_operational_job(self):
        snapshot, policy = self.snapshot()
        first = gate.evaluate(snapshot, policy)
        self.assertTrue(first["allow"], first)
        previous = snapshot["checks"][-1]
        previous.update(status="completed", conclusion="failure", completed_at=LATER)
        current = copy.deepcopy(previous)
        current.update(id=303, status="in_progress", conclusion=None, completed_at=None)
        current["_workflow"]["run_attempt"] = 2
        snapshot["checks"].append(current)
        run = snapshot["workflows"][-1]
        run["run_attempt"] = 2
        run["_jobs"].append(
            {
                "id": 303,
                "name": "maintenance-gate",
                "run_attempt": 2,
                "check_run_url": "https://api.github.com/repos/example/integration/check-runs/303",
            }
        )
        final = gate.evaluate(snapshot, policy)
        self.assertTrue(final["allow"], final)
        self.assertNotIn("check:303", {item["source"] for item in final["feedback"]})
        for key in ("inventory_digest", "snapshot_digest", "handling_digests"):
            self.assertEqual(first[key], final[key])

    def test_wrong_native_identity_and_policy_wildcard_are_rejected(self):
        for change in (
            {"app_id": 57789},
            {"name": "maintenance-*"},
            {"workflow_path": ".github/workflows/other.yml"},
        ):
            snapshot, policy = self.snapshot()
            policy["operational_check"].update(change)
            self.assertIn("invalid_evidence", fixtures.codes(snapshot, policy))
        snapshot, policy = self.snapshot()
        snapshot["checks"][-1]["app"]["id"] = 57789
        fixtures.seal(snapshot, policy)
        self.assertIn("unknown_check_context", fixtures.codes(snapshot, policy))

    def test_collector_accepts_only_self_changes_between_complete_reads(self):
        snapshot, policy = self.snapshot()
        normal = fixtures.ClientTests().full_transport(snapshot, policy)
        counts = {"checks": 0, "suites": 0, "runs": 0}

        def transport(url, query):
            data, headers = normal(url, query)
            category = (
                "checks"
                if "/check-runs?" in url
                else (
                    "suites"
                    if "/check-suites?" in url
                    else "runs" if "/actions/runs?" in url else None
                )
            )
            if category:
                counts[category] += 1
                if counts[category] == 2:
                    field = {
                        "checks": "check_runs",
                        "suites": "check_suites",
                        "runs": "workflow_runs",
                    }[category]
                    for item in data[field]:
                        if item["id"] in {301, 302, 12, 88}:
                            item.update(status="completed", conclusion="success")
            return data, headers

        collected, actual = gate.GitHub(REPOSITORY, "fixture", transport).collect(1)
        self.assertTrue(gate.evaluate(collected, actual)["allow"])


if __name__ == "__main__":
    unittest.main()
