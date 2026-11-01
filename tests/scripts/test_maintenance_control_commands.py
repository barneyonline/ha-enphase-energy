"""Owner identity, scope and operation constraints for the no-secret dispatch bridge."""

from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/maintenance/control_commands.py"
SPEC = importlib.util.spec_from_file_location("maintenance_control_commands", SCRIPT)
assert SPEC and SPEC.loader
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)
REPOSITORY = "example/integration"
SHA = "a" * 40


def policy() -> dict:
    return {
        "schema": 1,
        "repository": REPOSITORY,
        "default_branch": "main",
        "control_issue_number": 42,
        "control_label": "automation/control",
        "actor": {"id": 38914183, "login": "barneyonline"},
        "enabled_operations": ["candidate", "gate"],
    }


def event(command: dict | None = None) -> dict:
    command = command or {
        "schema": 1,
        "operation": "candidate",
        "candidate_sha": SHA,
        "version": "v1.2.4",
    }
    return {
        "action": "created",
        "repository": {"full_name": REPOSITORY},
        "sender": {"id": 38914183, "login": "barneyonline"},
        "issue": {
            "number": 42,
            "state": "open",
            "labels": [{"name": "automation/control"}],
        },
        "comment": {
            "id": 123,
            "created_at": "2026-11-01T00:00:00Z",
            "updated_at": "2026-11-01T00:00:00Z",
            "issue_url": "https://api.github.com/repos/example/integration/issues/42",
            "user": {"id": 38914183, "login": "barneyonline"},
            "body": "<!-- hal-control-request:v1 " + json.dumps(command) + " -->",
        },
    }


class ControlPlanTests(unittest.TestCase):
    def test_foreign_initiator_and_edited_or_rehomed_owner_commands_refuse(self):
        for sender in [
            None,
            {},
            {"id": 1, "login": "barneyonline"},
            {"id": 38914183, "login": "imposter"},
        ]:
            current = event()
            if sender is None:
                current.pop("sender")
            else:
                current["sender"] = sender
            self.assertEqual(
                "ignore", control.plan_dispatch(current, policy(), REPOSITORY)["action"]
            )
        for mutate in [
            lambda e: e["comment"].pop("created_at"),
            lambda e: e["comment"].update(created_at=1),
            lambda e: e["comment"].update(created_at="bad"),
            lambda e: e["comment"].update(updated_at="2026-11-02T00:00:00Z"),
            lambda e: e["comment"].update(issue_url="https://evil.test/issues/42"),
            lambda e: e["comment"].update(
                created_at="2026-99-01T00:00:00Z", updated_at="2026-99-01T00:00:00Z"
            ),
        ]:
            current = event()
            mutate(current)
            with self.assertRaises(ValueError):
                control.plan_dispatch(current, policy(), REPOSITORY)

    def test_candidate_plan_targets_only_fixed_workflow_and_branch(self) -> None:
        plan = control.plan_dispatch(event(), policy(), REPOSITORY)
        self.assertEqual("release-candidate.yml", plan["workflow"])
        self.assertEqual("main", plan["ref"])
        self.assertEqual(
            {"candidate_sha": SHA, "version": "v1.2.4", "control_request_id": "123"},
            plan["inputs"],
        )

    def test_closed_control_record_accepts_only_the_same_owner_operations(self) -> None:
        for command in (
            {
                "schema": 1,
                "operation": "candidate",
                "candidate_sha": SHA,
                "version": "v1.2.4",
            },
            {"schema": 1, "operation": "gate", "pull_number": 10},
        ):
            current = event(command)
            current["issue"]["state"] = "closed"
            expected = control.plan_dispatch(event(command), policy(), REPOSITORY)
            self.assertEqual(
                expected, control.plan_dispatch(current, policy(), REPOSITORY)
            )
            current["comment"]["user"]["id"] = 1
            self.assertEqual(
                "ignore", control.plan_dispatch(current, policy(), REPOSITORY)["action"]
            )
            current["comment"]["user"]["id"] = 38914183
            current["issue"]["labels"] = []
            with self.assertRaises(ValueError):
                control.plan_dispatch(current, policy(), REPOSITORY)

    def test_gate_plan_targets_only_one_pull_request(self) -> None:
        plan = control.plan_dispatch(
            event({"schema": 1, "operation": "gate", "pull_number": 10}),
            policy(),
            REPOSITORY,
        )
        self.assertEqual("maintenance-gate.yml", plan["workflow"])
        self.assertEqual(
            {"pull_number": "10", "control_request_id": "123"}, plan["inputs"]
        )

    def test_unrelated_invalid_state_and_pull_request_events_are_ignored(self) -> None:
        for mutate in [
            lambda e: e.update(action="edited"),
            lambda e: e["repository"].update(full_name="other/repository"),
            lambda e: e["issue"].update(number=99),
            lambda e: e["issue"].update(state="unknown"),
            lambda e: e["issue"].pop("state"),
            lambda e: e["issue"].update(pull_request={}),
        ]:
            current = event()
            mutate(current)
            self.assertEqual(
                "ignore", control.plan_dispatch(current, policy(), REPOSITORY)["action"]
            )

    def test_label_and_both_actor_identifiers_are_required(self) -> None:
        current = event()
        current["issue"]["labels"] = []
        with self.assertRaises(ValueError):
            control.plan_dispatch(current, policy(), REPOSITORY)
        for actor in [
            {"id": 1, "login": "barneyonline"},
            {"id": 38914183, "login": "imposter"},
        ]:
            current = event()
            current["comment"]["user"] = actor
            self.assertEqual(
                "ignore", control.plan_dispatch(current, policy(), REPOSITORY)["action"]
            )

    def test_standalone_bounded_marker_and_schema_are_required(self) -> None:
        for body in [
            None,
            "<!-- hal-control-request:v1 " + "x" * 4097,
            event()["comment"]["body"] + " arbitrary instructions",
            '<!-- hal-control-request:v2 {"schema":1,"operation":"gate","pull_number":1} -->',
            '<!-- hal-control-request:v1 {"schema":true,"operation":"gate","pull_number":1} -->',
            '<!-- hal-control-request:v1 {"schema":2,"operation":"gate","pull_number":1} -->',
        ]:
            current = event()
            current["comment"]["body"] = body
            with self.subTest(body=body), self.assertRaises(ValueError):
                control.plan_dispatch(current, policy(), REPOSITORY)

    def test_ordinary_owner_notes_do_not_dispatch_or_fail(self) -> None:
        for body in ["Cloud connectivity probe completed.", "x" * 4097]:
            current = event()
            current["comment"]["body"] = body
            self.assertEqual(
                "ignore", control.plan_dispatch(current, policy(), REPOSITORY)["action"]
            )

    def test_duplicate_json_fields_and_publication_are_rejected(self) -> None:
        current = event()
        current["comment"][
            "body"
        ] = '<!-- hal-control-request:v1 {"schema":1,"schema":1,"operation":"gate","pull_number":1} -->'
        with self.assertRaises(ValueError):
            control.plan_dispatch(current, policy(), REPOSITORY)
        with self.assertRaisesRegex(ValueError, "disabled"):
            control.plan_dispatch(
                event({"schema": 1, "operation": "publish"}), policy(), REPOSITORY
            )

    def test_command_cannot_select_arbitrary_workflow_or_inputs(self) -> None:
        commands = [
            {
                "schema": 1,
                "operation": "candidate",
                "candidate_sha": "main",
                "version": "v1.2.4",
            },
            {
                "schema": 1,
                "operation": "candidate",
                "candidate_sha": SHA,
                "version": "../../payload",
            },
            {
                "schema": 1,
                "operation": "candidate",
                "candidate_sha": SHA,
                "version": "v1.2.4",
                "workflow": "release-assets.yml",
            },
            {"schema": 1, "operation": "gate", "pull_number": True},
            {"schema": 1, "operation": "gate", "pull_number": -1},
            {"schema": 1, "operation": "gate", "pull_number": "10"},
        ]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(ValueError):
                control.plan_dispatch(event(command), policy(), REPOSITORY)
        current = event()
        current["comment"]["id"] = 0
        with self.assertRaises(ValueError):
            control.plan_dispatch(current, policy(), REPOSITORY)

    def test_policy_has_exact_scope_actor_and_enabled_operations(self) -> None:
        invalid = [
            {"schema": True},
            {"repository": "other/repository"},
            {"default_branch": "untrusted"},
            {"control_issue_number": 0},
            {"control_label": "arbitrary"},
            {"actor": {"id": True, "login": "barneyonline"}},
            {"actor": {"id": 1, "login": ""}},
            {"enabled_operations": ["publish"]},
            {"enabled_operations": ["gate", "gate"]},
            {"enabled_operations": []},
            {"unknown": True},
        ]
        for update in invalid:
            current = copy.deepcopy(policy())
            current.update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                control.plan_dispatch(event(), current, REPOSITORY)


class ControlCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.event_path = self.root / "event.json"
        self.policy_path = self.root / "policy.json"
        self.event_path.write_text(json.dumps(event()))
        self.policy_path.write_text(json.dumps(policy()))

    def args(self) -> list[str]:
        return [
            "control_commands.py",
            "--event",
            str(self.event_path),
            "--policy",
            str(self.policy_path),
            "--repository",
            REPOSITORY,
        ]

    def test_cli_prints_only_dispatch_metadata_without_executing_it(self) -> None:
        output = io.StringIO()
        with patch.object(sys, "argv", self.args()), redirect_stdout(output):
            self.assertEqual(0, control.main())
        self.assertEqual("dispatch", json.loads(output.getvalue())["action"])
        self.assertNotIn("comment", json.loads(output.getvalue()))

    def test_malformed_or_missing_inputs_fail_closed(self) -> None:
        for contents in ["not JSON", "[]"]:
            self.event_path.write_text(contents)
            with (
                self.subTest(contents=contents),
                patch.object(sys, "argv", self.args()),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(1, control.main())
        self.event_path.unlink()
        with patch.object(sys, "argv", self.args()), redirect_stdout(io.StringIO()):
            self.assertEqual(1, control.main())
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                sys, "argv", ["control_commands.py", "--policy", str(self.policy_path)]
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(1, control.main())

    def test_script_entrypoint_reports_success_without_network(self) -> None:
        with (
            patch.object(sys, "argv", self.args()),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as result,
        ):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(0, result.exception.code)


if __name__ == "__main__":
    unittest.main()
