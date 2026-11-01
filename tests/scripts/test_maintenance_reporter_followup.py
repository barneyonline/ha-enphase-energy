"""Exercise explicit reporter clocks without a live GitHub account."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import runpy
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from urllib.error import HTTPError

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts/maintenance/reporter_followup.py"
)
SPEC = importlib.util.spec_from_file_location("reporter_followup", SCRIPT)
assert SPEC and SPEC.loader
followup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(followup)

START = datetime(2026, 1, 1, tzinfo=UTC)
ORIGINAL_BODY = "Original report"


def comment(
    identifier: int,
    actor: str,
    body: str,
    day: int = 0,
    updated_day: int | None = None,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "user": {"login": actor},
        "body": body,
        "created_at": (START + timedelta(days=day)).isoformat(),
        "updated_at": (
            START + timedelta(days=updated_day if updated_day is not None else day)
        ).isoformat(),
    }


def state(
    identifier: int = 1, status: str = "awaiting-reporter", day: int = 0
) -> dict[str, Any]:
    payload = {
        "issue_number": 42,
        "status": status,
        "requested_at": (START + timedelta(days=day)).isoformat(),
        "issue_body_sha256": hashlib.sha256(ORIGINAL_BODY.encode()).hexdigest(),
    }
    return comment(
        identifier,
        "barneyonline",
        f"{followup.STATE_MARKER}\n{json.dumps(payload)}\n-->",
        day,
    )


def issue() -> dict[str, Any]:
    return {
        "number": 42,
        "state": "open",
        "body": ORIGINAL_BODY,
        "user": {"login": "reporter"},
        "labels": [{"name": followup.WAITING_LABEL}],
    }


class ReporterClockTests(unittest.TestCase):
    def decide(
        self,
        comments: list[dict[str, Any]],
        day: int,
        row: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return followup.followup_decision(
            row or issue(), comments, START + timedelta(days=day)
        )

    def test_requires_explicit_label_and_trusted_marker(self) -> None:
        self.assertEqual("none", self.decide([], 90)["action"])
        row = issue()
        row["labels"] = [{"name": "status/needs-diagnostics"}]
        self.assertEqual("none", self.decide([state()], 90, row)["action"])
        untrusted = state()
        untrusted["user"]["login"] = "stranger"
        self.assertEqual("none", self.decide([untrusted], 90)["action"])

    def test_closed_issue_and_pull_requests_never_expire(self) -> None:
        for field, value in [("state", "closed"), ("pull_request", {})]:
            with self.subTest(field=field):
                row = issue()
                row[field] = value
                self.assertEqual("none", self.decide([state()], 90, row)["action"])

    def test_reminds_at_seven_days_and_closes_at_twenty_one(self) -> None:
        self.assertEqual("none", self.decide([state()], 6)["action"])
        self.assertEqual("remind", self.decide([state()], 7)["action"])
        self.assertEqual("close", self.decide([state()], 21)["action"])

    def test_bot_or_other_contributor_activity_does_not_reset_clock(self) -> None:
        comments = [
            state(),
            comment(2, "codecov[bot]", "Coverage updated", 20),
            comment(3, "helper", "Any update?", 20),
        ]
        self.assertEqual("close", self.decide(comments, 21)["action"])

    def test_reporter_reply_or_edited_comment_cancels_closure(self) -> None:
        for reply in [
            comment(2, "reporter", "Here are the logs", 20),
            comment(2, "reporter", "Added requested logs", -1, 20),
        ]:
            with self.subTest(reply=reply):
                self.assertEqual(
                    "reporter-replied", self.decide([state(), reply], 21)["action"]
                )

    def test_owner_authored_issue_ignores_tagged_hal_updates_but_counts_real_reply(
        self,
    ) -> None:
        row = issue()
        row["user"]["login"] = "barneyonline"
        automated = comment(
            2,
            "barneyonline",
            f"{followup.AUTOMATED_UPDATE_MARKER}\nAutomated maintenance update\nStill awaiting details.",
            20,
        )
        request = state()
        request["updated_at"] = (START + timedelta(days=20)).isoformat()
        self.assertEqual("close", self.decide([request, automated], 21, row)["action"])
        genuine = comment(3, "barneyonline", "Here are the requested details.", 20)
        self.assertEqual(
            "reporter-replied",
            self.decide([request, automated, genuine], 21, row)["action"],
        )

    def test_untrusted_reporter_using_automated_tag_still_counts_as_a_response(
        self,
    ) -> None:
        response = comment(
            2,
            "reporter",
            f"{followup.AUTOMATED_UPDATE_MARKER}\nRequested information",
            20,
        )
        self.assertEqual(
            "reporter-replied", self.decide([state(), response], 21)["action"]
        )

    def test_issue_body_edit_cancels_closure(self) -> None:
        row = issue()
        row["body"] = "Original report with requested diagnostics"
        self.assertEqual("reporter-replied", self.decide([state()], 21, row)["action"])

    def test_missing_or_invalid_body_fingerprint_blocks_closure(self) -> None:
        for fingerprint in [None, "not-a-hash", 42]:
            with self.subTest(fingerprint=fingerprint):
                request = state()
                payload = followup.marker_payload(
                    request["body"], followup.STATE_MARKER
                )
                payload["issue_body_sha256"] = fingerprint
                request["body"] = f"{followup.STATE_MARKER}\n{json.dumps(payload)}\n-->"
                self.assertEqual("blocked", self.decide([request], 21)["action"])

    def test_newer_nonwaiting_state_cancels_closure(self) -> None:
        for status in [
            "investigating",
            "awaiting-review",
            "awaiting-release",
            "awaiting-validation",
        ]:
            with self.subTest(status=status):
                self.assertEqual(
                    "none", self.decide([state(), state(2, status, 1)], 90)["action"]
                )

    def test_edited_current_state_is_observed(self) -> None:
        waiting = state()
        waiting["body"] = state(status="investigating")["body"]
        self.assertEqual("none", self.decide([waiting], 90)["action"])

    def test_new_request_restarts_period(self) -> None:
        self.assertEqual("none", self.decide([state(), state(2, day=20)], 21)["action"])

    def test_reminder_is_idempotent_for_the_current_request(self) -> None:
        marker = f'{followup.REMINDER_MARKER}\n{{"request_comment_id": 1}}\n-->'
        self.assertEqual(
            "none",
            self.decide([state(), comment(2, "github-actions[bot]", marker, 7)], 8)[
                "action"
            ],
        )
        self.assertEqual(
            "close",
            self.decide([state(), comment(2, "github-actions[bot]", marker, 7)], 21)[
                "action"
            ],
        )

    def test_untrusted_reminder_does_not_suppress_real_reminder(self) -> None:
        marker = f'{followup.REMINDER_MARKER}\n{{"request_comment_id": 1}}\n-->'
        self.assertEqual(
            "remind",
            self.decide([state(), comment(2, "stranger", marker, 7)], 8)["action"],
        )

    def test_existing_closure_receipt_avoids_duplicate_closing_message(self) -> None:
        marker = f'{followup.CLOSE_MARKER}\n{{"request_comment_id": 1}}\n-->'
        decision = self.decide(
            [state(), comment(2, "github-actions[bot]", marker, 21)], 22
        )
        self.assertEqual("close", decision["action"])
        self.assertEqual(2, decision["close_comment_id"])

    def test_receipts_for_an_old_request_do_not_suppress_a_new_request(self) -> None:
        reminder = f'{followup.REMINDER_MARKER}\n{{"request_comment_id": 1}}\n-->'
        self.assertEqual(
            "remind",
            self.decide(
                [
                    state(),
                    state(2, day=1),
                    comment(3, "github-actions[bot]", reminder, 7),
                ],
                8,
            )["action"],
        )
        closure = f'{followup.CLOSE_MARKER}\n{{"request_comment_id": 1}}\n-->'
        result = self.decide(
            [state(), comment(3, "github-actions[bot]", closure, 21), state(2, day=22)],
            43,
        )
        self.assertEqual("close", result["action"])
        self.assertIsNone(result["close_comment_id"])

    def test_malformed_new_state_blocks_instead_of_using_old_state(self) -> None:
        invalid_bodies = [
            f"{followup.STATE_MARKER}\nnot json\n-->",
            f"{followup.STATE_MARKER}\n[]\n-->",
            f'{followup.STATE_MARKER}\n{{"issue_number": 99, "status": "awaiting-reporter"}}\n-->',
            f'{followup.STATE_MARKER}\n{{"issue_number": 42, "status": "unknown"}}\n-->',
            f"{followup.STATE_MARKER}\n{{}}",
            f"{followup.STATE_MARKER}\n{{}}\n-->{followup.STATE_MARKER}\n{{}}\n-->",
        ]
        for body in invalid_bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    "blocked",
                    self.decide([state(), comment(2, "barneyonline", body, 1)], 90)[
                        "action"
                    ],
                )

    def test_missing_naive_and_future_request_times_block(self) -> None:
        for value in [None, "2026-01-01", "2027-01-01T00:00:00Z"]:
            with self.subTest(value=value):
                payload = {
                    "issue_number": 42,
                    "status": "awaiting-reporter",
                    "requested_at": value,
                }
                body = f"{followup.STATE_MARKER}\n{json.dumps(payload)}\n-->"
                self.assertEqual(
                    "blocked",
                    self.decide([comment(1, "barneyonline", body)], 90)["action"],
                )

    def test_backdated_timestamp_cannot_close_before_comment_age(self) -> None:
        request = state(day=20)
        request["body"] = state()["body"]
        self.assertEqual("none", self.decide([request], 21)["action"])

    def test_invalid_comment_date_fails_closed(self) -> None:
        request = state()
        request["created_at"] = "broken"
        self.assertEqual("blocked", self.decide([request], 90)["action"])

    def test_invalid_request_id_or_issue_body_fails_closed(self) -> None:
        request = state()
        request["id"] = 0
        self.assertEqual("blocked", self.decide([request], 90)["action"])
        for invalid in [42, 0, False]:
            row = issue()
            row["body"] = invalid
            self.assertEqual("blocked", self.decide([state()], 90, row)["action"])

    def test_null_issue_body_uses_explicit_empty_fingerprint(self) -> None:
        row = issue()
        row["body"] = None
        request = state()
        payload = followup.marker_payload(request["body"], followup.STATE_MARKER)
        payload["issue_body_sha256"] = hashlib.sha256(b"").hexdigest()
        request["body"] = f"{followup.STATE_MARKER}\n{json.dumps(payload)}\n-->"
        self.assertEqual("close", self.decide([request], 90, row)["action"])

    def test_timestamp_requires_timezone_and_string(self) -> None:
        for value in [3, "2026-01-01"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                followup.timestamp(value)


class FollowupMutationTests(unittest.TestCase):
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def remove_label(self, number: int, label: str) -> None:
            self.calls.append(("remove-label", number, label))

        def request(self, method: str, path: str, data: dict[str, Any]) -> None:
            self.calls.append((method, path, data))

    def test_reply_removes_waiting_and_legacy_stale_labels(self) -> None:
        client = self.FakeClient()
        followup.apply_followup(client, issue(), {"action": "reporter-replied"})
        self.assertEqual(
            [
                ("remove-label", 42, followup.WAITING_LABEL),
                ("remove-label", 42, "status/stale"),
            ],
            client.calls,
        )

    def test_reminder_contains_deadline_and_deduplication_marker(self) -> None:
        client = self.FakeClient()
        decision = {
            "action": "remind",
            "deadline": "2026-01-22T00:00:00+00:00",
            "request_comment_id": 1,
        }
        followup.apply_followup(client, issue(), decision)
        self.assertEqual("POST", client.calls[0][0])
        self.assertIn("2026-01-22", client.calls[0][2]["body"])
        self.assertIn(followup.REMINDER_MARKER, client.calls[0][2]["body"])

    def test_incomplete_comment_precedes_closure(self) -> None:
        client = self.FakeClient()
        followup.apply_followup(
            client, issue(), {"action": "close", "request_comment_id": 1}
        )
        self.assertEqual("POST", client.calls[0][0])
        self.assertIn("reopen", client.calls[0][2]["body"])
        self.assertEqual(
            (
                "PATCH",
                "/issues/42",
                {
                    "state": "closed",
                    "state_reason": "not_planned",
                    "labels": ["status/closed-incomplete"],
                },
            ),
            client.calls[1],
        )

    def test_retry_preserves_closure_notice_and_changes_only_lifecycle_labels(
        self,
    ) -> None:
        client = self.FakeClient()
        row = issue()
        row["labels"].append({"name": "area/solar"})
        followup.apply_followup(
            client,
            row,
            {"action": "close", "request_comment_id": 1, "close_comment_id": 2},
        )
        self.assertEqual(1, len(client.calls))
        self.assertEqual(
            ["area/solar", "status/closed-incomplete"], client.calls[0][2]["labels"]
        )

    def test_no_action_performs_no_mutation(self) -> None:
        client = self.FakeClient()
        followup.apply_followup(client, issue(), {"action": "none"})
        self.assertEqual([], client.calls)


class FollowupTransportTests(unittest.TestCase):
    def test_rejects_bad_repository_and_redirects(self) -> None:
        with self.assertRaises(ValueError):
            followup.GitHub("https://elsewhere.invalid/repository", "fixture")
        with self.assertRaises(ValueError):
            followup.NoRedirect().redirect_request(
                None, None, 302, "redirect", {}, "https://elsewhere.invalid"
            )

    def test_requests_fixed_origin_and_parses_json_or_empty_responses(self) -> None:
        client = followup.GitHub("example/repository", "fixture")
        response = Mock()
        response.read.side_effect = [b'{"ok": true}', b""]
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(followup, "build_opener", return_value=opener):
            self.assertEqual(
                {"ok": True},
                client.request("POST", "/issues/42/comments", {"body": "fixture"}),
            )
            self.assertIsNone(client.request("DELETE", "/issues/42/labels/test"))
        request = opener.open.call_args_list[0].args[0]
        self.assertEqual(
            "https://api.github.com/repos/example/repository/issues/42/comments",
            request.full_url,
        )
        self.assertEqual({"body": "fixture"}, json.loads(request.data))

    def test_pagination_including_existing_query_and_incomplete_response(self) -> None:
        client = followup.GitHub("example/repository", "fixture")
        with patch.object(
            client, "request", side_effect=[[{"id": 1}] * 100, [{"id": 2}]]
        ):
            self.assertEqual(101, len(client.list("/issues?state=open")))
        with (
            patch.object(client, "request", return_value={"message": "wrong shape"}),
            self.assertRaises(ValueError),
        ):
            client.list("/issues")
        with (
            patch.object(client, "request", return_value=[{}] * 100),
            self.assertRaisesRegex(ValueError, "Pagination limit"),
        ):
            client.list("/issues")

    def test_missing_label_is_idempotent_but_permission_errors_propagate(self) -> None:
        client = followup.GitHub("example/repository", "fixture")
        for status in [404, 403]:
            error = HTTPError(
                "https://api.github.com/example", status, "fixture", {}, None
            )
            self.addCleanup(error.close)
            with (
                self.subTest(status=status),
                patch.object(client, "request", side_effect=error),
            ):
                if status == 404:
                    client.remove_label(42, "status/awaiting-reporter")
                else:
                    with self.assertRaises(HTTPError):
                        client.remove_label(42, "status/awaiting-reporter")


class FollowupCommandTests(unittest.TestCase):
    def invoke(self, client: Mock, apply: bool = False) -> str:
        args = ["reporter_followup.py", "--repository", "example/repository"]
        if apply:
            args.append("--apply")
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"GH_TOKEN": "fixture"}, clear=True),
            patch.object(sys, "argv", args),
            patch.object(followup, "GitHub", return_value=client),
            patch.object(followup, "datetime", wraps=datetime) as clock,
            redirect_stdout(output),
        ):
            clock.now.return_value = START + timedelta(days=30)
            followup.main()
        return output.getvalue()

    def test_dry_run_is_read_only_and_skips_pull_requests(self) -> None:
        client = Mock()
        client.list.side_effect = [
            [{"number": 1, "pull_request": {}}, issue()],
            [state()],
        ]
        output = self.invoke(client)
        self.assertEqual("close", json.loads(output)["action"])
        client.request.assert_not_called()

    def test_apply_rechecks_evidence_before_closing(self) -> None:
        client = Mock()
        client.list.side_effect = [[issue()], [state()], [state()]]
        client.request.return_value = issue()
        self.invoke(client, apply=True)
        methods = [call.args[0] for call in client.request.call_args_list]
        self.assertEqual(["GET", "POST", "PATCH"], methods)

    def test_reply_arriving_during_recheck_cancels_closure(self) -> None:
        client = Mock()
        client.list.side_effect = [
            [issue()],
            [state()],
            [state(), comment(2, "reporter", "Requested details", 29)],
        ]
        client.request.return_value = issue()
        self.invoke(client, apply=True)
        self.assertEqual(
            ["GET"], [call.args[0] for call in client.request.call_args_list]
        )

    def test_invalid_state_blocks_the_command_without_mutation(self) -> None:
        client = Mock()
        client.list.side_effect = [
            [issue()],
            [comment(1, "barneyonline", followup.STATE_MARKER + " malformed")],
        ]
        with self.assertRaises(SystemExit):
            self.invoke(client, apply=True)
        client.request.assert_not_called()

    def test_missing_credentials_and_script_entrypoint_fail_before_network(
        self,
    ) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys, "argv", ["reporter_followup.py"]),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            runpy.run_path(str(SCRIPT), run_name="__main__")


if __name__ == "__main__":
    unittest.main()
