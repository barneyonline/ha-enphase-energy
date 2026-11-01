"""Adversarial proof contracts for explicitly approved zero-run app suites."""

from __future__ import annotations

import copy
import unittest

import test_maintenance_pr_gate as fixtures

gate = fixtures.gate
APP = {"app_id": 222, "slug": "unused-check-app"}
PREFIX = f"https://api.github.com/repos/{fixtures.REPOSITORY}/"
SUITE_ID = 12
REQUEST = f"{PREFIX}check-suites/{SUITE_ID}/check-runs?filter=all&per_page=100"


def empty_fixture():
    snapshot, policy = fixtures.fixture()
    policy["empty_check_suite_apps"] = [dict(APP)]
    repository = {"id": 777, "full_name": fixtures.REPOSITORY}
    snapshot["pr"]["base"]["repo"] = dict(repository)
    for workflow in snapshot["workflows"]:
        workflow["check_suite_id"] = 11
        workflow["check_suite_url"] = PREFIX + "check-suites/11"
    suite = {
        "id": SUITE_ID,
        "head_sha": fixtures.HEAD,
        "repository": dict(repository),
        "app": {"id": APP["app_id"], "slug": APP["slug"]},
        "status": "queued",
        "conclusion": None,
        "latest_check_runs_count": 0,
        "url": PREFIX + f"check-suites/{SUITE_ID}",
        "check_runs_url": REQUEST.split("?", 1)[0],
        "updated_at": fixtures.WHEN,
        "_empty_runs_proof": {
            "method": "GET",
            "request_url": REQUEST,
            "repository": dict(repository),
            "head_sha": fixtures.HEAD,
            "app": {"id": APP["app_id"], "slug": APP["slug"]},
            "suite_id": SUITE_ID,
            "response": {"total_count": 0, "check_runs": []},
            "link_headers": [],
        },
    }
    snapshot["check_suites"].append(suite)
    fixtures.seal(snapshot, policy)
    fixtures.attest(snapshot, policy)
    return snapshot, policy


def transport_for(snapshot, policy, response=None, headers=None):
    ordinary = fixtures.ClientTests().full_transport(snapshot, policy)
    requests = []

    def transport(url, query):
        if url == REQUEST:
            requests.append(url)
            return copy.deepcopy(
                response
                if response is not None
                else {"total_count": 0, "check_runs": []}
            ), dict(headers or {})
        data, response_headers = ordinary(url, query)
        if "/check-suites?" in url:
            for suite in data["check_suites"]:
                suite.pop("_empty_runs_proof", None)
        return data, response_headers

    return transport, requests


class EmptySuitePolicyTests(unittest.TestCase):
    def test_optional_field_absent_or_empty_preserves_existing_fixture(self):
        for present in (False, True):
            snapshot, policy = fixtures.fixture()
            if present:
                policy["empty_check_suite_apps"] = []
                fixtures.seal(snapshot, policy)
                fixtures.attest(snapshot, policy)
            self.assertTrue(gate.evaluate(snapshot, policy)["allow"])

    def test_app_policy_rejects_malformed_duplicate_reserved_and_check_apps(self):
        invalid = [
            None,
            {},
            [None],
            [{"app_id": 222}],
            [{"app_id": 222, "slug": "unused", "extra": True}],
            [{"app_id": True, "slug": "unused"}],
            [{"app_id": 0, "slug": "unused"}],
            [{"app_id": 222, "slug": ""}],
            [{"app_id": 222, "slug": "Uppercase"}],
            [{"app_id": 222, "slug": "not/a-slug"}],
            [{"app_id": 15368, "slug": "github-actions"}],
            [dict(APP), dict(APP)],
            [dict(APP), {"app_id": 223, "slug": APP["slug"]}],
            [dict(APP), {"app_id": 222, "slug": "different"}],
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(gate.GateError):
                _, policy = fixtures.fixture()
                policy["empty_check_suite_apps"] = value
                gate.validate_policy(policy)
        _, policy = fixtures.fixture()
        policy["checks"].append(
            {
                "app_id": 222,
                "workflow_path": None,
                "name": "optional",
                "required": False,
            }
        )
        policy["empty_check_suite_apps"] = [dict(APP)]
        with self.assertRaises(gate.GateError):
            gate.validate_policy(policy)


class EmptySuiteEvaluationTests(unittest.TestCase):
    def test_unapproved_or_actions_app_with_null_slug_cannot_match_missing_policy(self):
        for app_id in (223, 15368):
            snapshot, policy = empty_fixture()
            suite = snapshot["check_suites"][-1]
            suite["app"] = {"id": app_id, "slug": None}
            suite["_empty_runs_proof"]["app"] = dict(suite["app"])
            with self.subTest(app_id=app_id):
                self.assertFalse(gate.evaluate(snapshot, policy)["allow"])

    def test_exact_empty_app_suite_is_retained_with_digest_bound_receipt(self):
        snapshot, policy = empty_fixture()
        result = gate.evaluate(snapshot, policy)
        self.assertTrue(result["allow"], result)
        receipt = result["empty_check_suite_receipts"][0]
        self.assertEqual(SUITE_ID, receipt["suite_id"])
        self.assertEqual(APP["app_id"], receipt["app_id"])
        self.assertEqual(APP["slug"], receipt["slug"])
        self.assertEqual(
            gate.digest(snapshot["check_suites"][-1]["_empty_runs_proof"]),
            receipt["proof_sha256"],
        )
        view = gate.decision_view(snapshot, policy)
        self.assertIn(snapshot["check_suites"][-1], view["check_suites"])
        self.assertEqual(
            result["empty_check_suite_receipts"], view["empty_check_suite_receipts"]
        )
        before = result["snapshot_digest"]
        snapshot["check_suites"][-1]["updated_at"] = fixtures.LATER
        self.assertNotEqual(before, gate.evaluate(snapshot, policy)["snapshot_digest"])

    def test_suite_scope_state_and_workflow_contradictions_block(self):
        changes = [
            lambda s: s["check_suites"][-1].pop("_empty_runs_proof"),
            lambda s: s["check_suites"][-1].update(
                app={"id": 223, "slug": APP["slug"]}
            ),
            lambda s: s["check_suites"][-1]["app"].update(slug="different"),
            lambda s: s["check_suites"][-1]["app"].update(id=True),
            lambda s: s["check_suites"][-1].update(app=None),
            lambda s: s["check_suites"][-1].update(head_sha="f" * 40),
            lambda s: s["check_suites"][-1]["repository"].update(id=778),
            lambda s: s["check_suites"][-1]["repository"].update(
                full_name="other/repo"
            ),
            lambda s: s["pr"]["base"].pop("repo"),
            lambda s: s["pr"]["base"]["repo"].update(full_name="other/repo"),
            lambda s: s["check_suites"][-1].update(status="in_progress"),
            lambda s: s["check_suites"][-1].update(
                status="completed", conclusion="success"
            ),
            lambda s: s["check_suites"][-1].pop("conclusion"),
            lambda s: s["check_suites"][-1].update(conclusion="failure"),
            lambda s: s["check_suites"][-1].update(latest_check_runs_count=True),
            lambda s: s["check_suites"][-1].update(latest_check_runs_count=1),
            lambda s: s["check_suites"][-1].update(url=PREFIX + "check-suites/99"),
            lambda s: s["check_suites"][-1].update(
                check_runs_url="https://other.invalid/runs"
            ),
            lambda s: s["workflows"][0].update(
                check_suite_id=SUITE_ID,
                check_suite_url=PREFIX + f"check-suites/{SUITE_ID}",
            ),
            lambda s: s["workflows"][0].pop("check_suite_id"),
            lambda s: s["workflows"][0].pop("check_suite_url"),
            lambda s: s["workflows"][0].update(
                check_suite_url=PREFIX + f"check-suites/{SUITE_ID}"
            ),
        ]
        for change in changes:
            snapshot, policy = empty_fixture()
            change(snapshot)
            with self.subTest(change=change):
                result = gate.evaluate(snapshot, policy)
                self.assertFalse(result["allow"], result)
                self.assertIn("invalid_evidence", fixtures.codes(snapshot, policy))

    def test_proof_shape_binding_counts_hidden_runs_and_pagination_block(self):
        changes = [
            lambda p: p.update(extra=True),
            lambda p: p.pop("method"),
            lambda p: p.update(method="POST"),
            lambda p: p.update(suite_id=99),
            lambda p: p.update(head_sha="f" * 40),
            lambda p: p["repository"].update(id=778),
            lambda p: p["repository"].update(full_name="other/repo"),
            lambda p: p["app"].update(id=223),
            lambda p: p["app"].update(slug="different"),
            lambda p: p.update(
                request_url=REQUEST.replace("filter=all", "filter=latest")
            ),
            lambda p: p.update(
                request_url=REQUEST.replace("per_page=100", "per_page=30")
            ),
            lambda p: p.update(response=None),
            lambda p: p.update(response={}),
            lambda p: p["response"].update(total_count=True),
            lambda p: p["response"].update(total_count=1),
            lambda p: p["response"].update(check_runs=[{"id": 987}]),
            lambda p: p["response"].update(check_runs=None),
            lambda p: p.update(
                link_headers=[
                    {
                        "name": "Link",
                        "value": '<https://api.github.com/next>; rel="next"',
                    }
                ]
            ),
            lambda p: p.update(link_headers=[{"name": "lInK", "value": "malformed"}]),
        ]
        for change in changes:
            snapshot, policy = empty_fixture()
            change(snapshot["check_suites"][-1]["_empty_runs_proof"])
            with self.subTest(change=change):
                self.assertIn("invalid_evidence", fixtures.codes(snapshot, policy))

    def test_emitted_pending_or_failed_checks_remain_unknown_feedback(self):
        for status, conclusion in (
            ("queued", None),
            ("completed", "failure"),
            ("completed", "success"),
        ):
            snapshot, policy = empty_fixture()
            check = copy.deepcopy(snapshot["checks"][0])
            check.update(
                id=987,
                app={"id": APP["app_id"], "slug": APP["slug"]},
                check_suite={"id": SUITE_ID},
                _workflow=None,
                status=status,
                conclusion=conclusion,
            )
            snapshot["checks"].append(check)
            fixtures.seal(snapshot, policy)
            fixtures.attest(snapshot, policy)
            result = gate.evaluate(snapshot, policy)
            self.assertFalse(result["allow"], result)
            self.assertIn("unknown_check_context", fixtures.codes(snapshot, policy))
            self.assertIn("check:987", {item["source"] for item in result["feedback"]})
            self.assertEqual([], result["empty_check_suite_receipts"])

    def test_empty_receipt_never_replaces_missing_or_failed_required_check(self):
        for absent in (False, True):
            snapshot, policy = empty_fixture()
            if absent:
                snapshot["checks"] = []
                snapshot["check_suites"] = [snapshot["check_suites"][-1]]
            else:
                snapshot["checks"][0]["conclusion"] = "failure"
            fixtures.seal(snapshot, policy)
            fixtures.attest(snapshot, policy)
            result = gate.evaluate(snapshot, policy)
            self.assertFalse(result["allow"], result)
            expected = (
                "required_check_missing" if absent else "latest_check_not_successful"
            )
            self.assertIn(expected, fixtures.codes(snapshot, policy))


class EmptySuiteCollectorTests(unittest.TestCase):
    def test_multiple_explicit_apps_get_distinct_proofs_in_each_sweep(self):
        snapshot, policy = empty_fixture()
        policy["empty_check_suite_apps"].append(
            {"app_id": 223, "slug": "second-unused-app"}
        )
        second = copy.deepcopy(snapshot["check_suites"][-1])
        second.update(
            id=13,
            app={"id": 223, "slug": "second-unused-app"},
            url=PREFIX + "check-suites/13",
            check_runs_url=PREFIX + "check-suites/13/check-runs",
        )
        second_request = PREFIX + "check-suites/13/check-runs?filter=all&per_page=100"
        second["_empty_runs_proof"].update(
            suite_id=13, app=dict(second["app"]), request_url=second_request
        )
        snapshot["check_suites"].insert(1, second)
        fixtures.seal(snapshot, policy)
        fixtures.attest(snapshot, policy)
        ordinary, first_requests = transport_for(snapshot, policy)
        second_requests = []

        def transport(url, query):
            if url == second_request:
                second_requests.append(url)
                return {"total_count": 0, "check_runs": []}, {}
            return ordinary(url, query)

        collected, actual = gate.GitHub(
            fixtures.REPOSITORY, "placeholder", transport
        ).collect(1)
        result = gate.evaluate(collected, actual)
        self.assertTrue(result["allow"], result)
        self.assertEqual([REQUEST, REQUEST], first_requests)
        self.assertEqual([second_request, second_request], second_requests)
        self.assertEqual(
            [12, 13],
            [item["suite_id"] for item in result["empty_check_suite_receipts"]],
        )
        self.assertEqual(
            [222, 223],
            [item["app_id"] for item in result["empty_check_suite_receipts"]],
        )

    def test_collect_once_without_trusted_policy_never_probes_or_waives_suite(self):
        snapshot, policy = empty_fixture()
        transport, requests = transport_for(snapshot, policy)
        collected = gate.GitHub(
            fixtures.REPOSITORY, "placeholder", transport
        ).collect_once(1)
        self.assertEqual([], requests)
        self.assertNotIn("_empty_runs_proof", collected["check_suites"][-1])
        collected["stable"] = True
        self.assertIn("invalid_evidence", fixtures.codes(collected, policy))

    def test_volatile_response_headers_never_enter_the_stable_proof(self):
        snapshot, policy = empty_fixture()
        ordinary, requests = transport_for(snapshot, policy)

        def transport(url, query):
            data, headers = ordinary(url, query)
            if url == REQUEST:
                headers.update(
                    {
                        "Date": str(len(requests)),
                        "X-GitHub-Request-Id": f"read-{len(requests)}",
                        "X-RateLimit-Remaining": str(100 - len(requests)),
                    }
                )
            return data, headers

        collected, actual = gate.GitHub(
            fixtures.REPOSITORY, "placeholder", transport
        ).collect(1)
        proof = collected["check_suites"][-1]["_empty_runs_proof"]
        self.assertEqual([], proof["link_headers"])
        self.assertNotIn("Date", gate.canonical(proof))
        self.assertTrue(gate.evaluate(collected, actual)["allow"])

    def test_each_complete_sweep_fetches_its_own_unfiltered_zero_run_proof(self):
        snapshot, policy = empty_fixture()
        transport, requests = transport_for(snapshot, policy)
        collected, actual = gate.GitHub(
            fixtures.REPOSITORY, "placeholder", transport
        ).collect(1)
        self.assertEqual([REQUEST, REQUEST], requests)
        self.assertEqual(
            snapshot["check_suites"][-1]["_empty_runs_proof"],
            collected["check_suites"][-1]["_empty_runs_proof"],
        )
        self.assertTrue(gate.evaluate(collected, actual)["allow"])

    def test_collector_rejects_nonempty_truncated_malformed_or_paginated_proof(self):
        cases = [
            ({}, {}),
            ({"total_count": 1, "check_runs": []}, {}),
            ({"total_count": 0, "check_runs": [{"id": 987}]}, {}),
            ({"total_count": False, "check_runs": []}, {}),
            ({"total_count": 0}, {}),
            (
                {"total_count": 0, "check_runs": []},
                {"Link": '<https://api.github.com/next>; rel="next"'},
            ),
            ({"total_count": 0, "check_runs": []}, {"lInK": "malformed"}),
        ]
        for response, headers in cases:
            snapshot, policy = empty_fixture()
            transport, _ = transport_for(snapshot, policy, response, headers)
            with self.subTest(response=response, headers=headers), self.assertRaises(
                gate.GateError
            ):
                gate.GitHub(fixtures.REPOSITORY, "placeholder", transport).collect(1)

    def test_suite_change_or_run_appearance_between_sweeps_blocks(self):
        for move in ("metadata", "runs"):
            snapshot, policy = empty_fixture()
            ordinary, requests = transport_for(snapshot, policy)
            suites_reads = 0

            def transport(url, query, ordinary=ordinary, move=move, requests=requests):
                nonlocal suites_reads
                data, headers = ordinary(url, query)
                if "/check-suites?" in url:
                    suites_reads += 1
                    if suites_reads == 2 and move == "metadata":
                        data["check_suites"][-1]["updated_at"] = fixtures.LATER
                if url == REQUEST and len(requests) == 2 and move == "runs":
                    data = {"total_count": 1, "check_runs": [{"id": 987}]}
                return data, headers

            with self.subTest(move=move), self.assertRaises(gate.GateError):
                gate.GitHub(fixtures.REPOSITORY, "placeholder", transport).collect(1)


if __name__ == "__main__":
    unittest.main()
