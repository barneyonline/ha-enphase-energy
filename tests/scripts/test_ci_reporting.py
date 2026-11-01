"""Provenance and hostile report-data regressions for the protected reporter."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

adjacent = Path(__file__).with_name("ci_reporting.py")
ROOT = next(
    (
        p
        for p in Path(__file__).resolve().parents
        if (p / ".github/workflows/coverage-reporting.yml").is_file()
    ),
    None,
)
MODULE = (
    adjacent if adjacent.is_file() else ROOT / "scripts/maintenance/ci_reporting.py"
)
spec = importlib.util.spec_from_file_location("ci_reporting", MODULE)
reporting = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporting)


class ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = "barneyonline/ha-groq"
        self.repo_id = reporting.REPOSITORIES[self.repository]
        self.head = "a" * 40
        prefix = "repos/" + self.repository
        self.event = {
            "repository": {"id": self.repo_id},
            "workflow_run": {"id": 42, "run_attempt": 2},
        }
        self.run = {
            "id": 42,
            "run_attempt": 2,
            "repository": {"id": self.repo_id},
            "head_repository": {"id": self.repo_id},
            "status": "completed",
            "conclusion": "success",
            "event": "pull_request",
            "path": ".github/workflows/tests.yml",
            "workflow_id": 50,
            "head_sha": self.head,
            "head_branch": "codex/issue-7",
            "run_started_at": "2026-11-01T10:00:00Z",
            "updated_at": "2026-11-01T10:02:00Z",
        }
        self.pr = {
            "number": 72,
            "state": "open",
            "head": {
                "sha": self.head,
                "ref": "codex/issue-7",
                "repo": {"id": self.repo_id},
            },
            "base": {"repo": {"id": self.repo_id}},
        }
        self.artifact = {
            "id": 99,
            "name": "coverage-evidence-2",
            "expired": False,
            "size_in_bytes": 512,
            "created_at": "2026-11-01T10:01:00Z",
            "workflow_run": {
                "id": 42,
                "repository_id": self.repo_id,
                "head_repository_id": self.repo_id,
                "head_sha": self.head,
            },
        }
        self.responses = {
            f"{prefix}/actions/runs/42": self.run,
            f"{prefix}/actions/workflows/tests.yml": {
                "id": 50,
                "path": ".github/workflows/tests.yml",
                "state": "active",
            },
            f"{prefix}/commits/{self.head}/pulls?per_page=100": [self.pr],
            f"{prefix}/pulls/72": self.pr,
            f"{prefix}/actions/runs/42/artifacts?per_page=100": {
                "total_count": 1,
                "artifacts": [self.artifact],
            },
        }
        self.job = {
            "name": "checks",
            "status": "completed",
            "conclusion": "success",
            "steps": [
                {
                    "name": "Run tests with coverage in Home Assistant Docker",
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        }
        self.responses[f"{prefix}/actions/runs/42/attempts/2/jobs?per_page=100"] = {
            "total_count": 1,
            "jobs": [self.job],
        }

    def verify(self) -> dict:
        return reporting.verify_scope(
            self.event,
            self.repository,
            lambda route: copy.deepcopy(self.responses[route]),
        )

    def test_complete_current_attempt_is_bound(self) -> None:
        result = self.verify()
        self.assertEqual(result["artifact_id"], 99)
        self.assertEqual(result["head_sha"], self.head)
        self.assertEqual(result["pr_number"], 72)

    def test_foreign_repository_is_refused(self) -> None:
        self.run["head_repository"]["id"] = 17
        with self.assertRaises(reporting.Refused):
            self.verify()

    def test_named_workflow_is_not_enough(self) -> None:
        self.run["workflow_id"] = 51
        self.run["name"] = "Tests"
        with self.assertRaises(reporting.Refused):
            self.verify()

    def test_other_workflow_path_is_refused(self) -> None:
        self.run["path"] = ".github/workflows/foreign.yml"
        with self.assertRaises(reporting.Refused):
            self.verify()

    def test_old_attempt_event_is_refused(self) -> None:
        self.event["workflow_run"]["run_attempt"] = 1
        with self.assertRaises(reporting.Refused):
            self.verify()

    def test_stale_artifact_with_current_name_is_refused(self) -> None:
        self.artifact["created_at"] = "2026-11-01T09:00:00Z"
        with self.assertRaises(reporting.Refused):
            self.verify()

    def test_forged_artifact_head_is_refused(self) -> None:
        self.artifact["workflow_run"]["head_sha"] = "b" * 40
        with self.assertRaises(reporting.Refused):
            self.verify()

    def test_incomplete_artifact_inventory_is_refused(self) -> None:
        self.responses[
            f"repos/{self.repository}/actions/runs/42/artifacts?per_page=100"
        ]["total_count"] = 2
        with self.assertRaises(reporting.Refused):
            self.verify()

    def test_pr_change_on_fresh_read_is_refused(self) -> None:
        latest = copy.deepcopy(self.pr)
        latest["head"]["sha"] = "b" * 40
        self.responses[f"repos/{self.repository}/pulls/72"] = latest
        with self.assertRaises(reporting.Refused):
            self.verify()

    def test_branch_output_cannot_inject_other_outputs(self) -> None:
        self.pr["head"]["ref"] = "branch\nartifact_id=100"
        with self.assertRaises(reporting.Refused):
            self.verify()

    def test_skipped_producer_needs_no_reports(self) -> None:
        self.job["conclusion"] = "skipped"
        del self.responses[
            f"repos/{self.repository}/actions/runs/42/artifacts?per_page=100"
        ]
        self.assertIs(self.verify()["eligible"], False)

    def test_successful_job_cannot_omit_the_test_step(self) -> None:
        self.job["steps"] = []
        with self.assertRaises(reporting.Refused):
            self.verify()

    def test_skipped_test_step_needs_no_reports(self) -> None:
        self.job["steps"][0]["conclusion"] = "skipped"
        self.assertIs(self.verify()["eligible"], False)

    def test_failed_test_step_still_requires_reports(self) -> None:
        self.job["steps"][0]["conclusion"] = "failure"
        self.artifact["name"] = "other"
        with self.assertRaises(reporting.Refused):
            self.verify()


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.reports = self.root / "reports"
        self.reports.mkdir()
        (self.reports / "coverage.xml").write_text("<coverage/>")
        (self.reports / "junit.xml").write_text("<testsuites/>")

    def test_valid_data_is_accepted(self) -> None:
        (self.reports / "junit.xml").write_text(
            '<testsuites><testsuite name="$(touch marker)"/></testsuites>'
        )
        self.assertTrue(reporting.validate_reports(self.reports)["coverage"])
        self.assertFalse((self.root / "marker").exists())

    def test_either_missing_report_is_refused(self) -> None:
        for name in ("coverage.xml", "junit.xml"):
            path = self.reports / name
            raw = path.read_bytes()
            path.unlink()
            with self.subTest(name=name), self.assertRaises(reporting.Refused):
                reporting.validate_reports(self.reports)
            path.write_bytes(raw)

    def test_extra_program_is_refused(self) -> None:
        (self.reports / "payload.py").write_text('raise RuntimeError("do not execute")')
        with self.assertRaises(reporting.Refused):
            reporting.validate_reports(self.reports)

    def test_entity_is_refused(self) -> None:
        (self.reports / "coverage.xml").write_text(
            '<!DOCTYPE coverage [<!ENTITY a SYSTEM "file:///etc/passwd">]><coverage>&a;</coverage>'
        )
        with self.assertRaises(reporting.Refused):
            reporting.validate_reports(self.reports)

    def test_special_file_is_refused_without_blocking(self) -> None:
        (self.reports / "coverage.xml").unlink()
        os.mkfifo(self.reports / "coverage.xml")
        with self.assertRaises(reporting.Refused):
            reporting.validate_reports(self.reports)

    def test_symlink_is_refused(self) -> None:
        (self.reports / "coverage.xml").unlink()
        (self.root / "elsewhere").write_text("<coverage/>")
        (self.reports / "coverage.xml").symlink_to(self.root / "elsewhere")
        with self.assertRaises(reporting.Refused):
            reporting.validate_reports(self.reports)

    def test_oversized_file_is_refused(self) -> None:
        with (self.reports / "coverage.xml").open("wb") as stream:
            stream.truncate(16_777_217)
        with self.assertRaises(reporting.Refused):
            reporting.validate_reports(self.reports)


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch.dict(os.environ, {"GH_TOKEN": "synthetic-unit-token"}, clear=True):
            self.api = reporting.ReadAPI("barneyonline/ha-groq")
        self.response = Mock()
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=False)
        self.response.geturl.return_value = (
            "https://api.github.com/repos/barneyonline/ha-groq/actions/runs/42"
        )
        self.response.read.return_value = b'{"id":42}'
        self.api.opener = Mock()
        self.api.opener.open.return_value = self.response

    def test_only_fixed_get_route_is_used(self) -> None:
        self.assertEqual(
            self.api("repos/barneyonline/ha-groq/actions/runs/42"), {"id": 42}
        )
        request = self.api.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, self.response.geturl.return_value)

    def test_foreign_or_arbitrary_routes_are_refused(self) -> None:
        for route in (
            "https://attacker.invalid/",
            "repos/barneyonline/ha-enphase-energy/actions/runs/42",
            "repos/barneyonline/ha-groq/issues/1",
            "repos/barneyonline/ha-groq/actions/runs/42#secret",
        ):
            with self.subTest(route=route), self.assertRaises(reporting.Refused):
                self.api(route)
        self.api.opener.open.assert_not_called()

    def test_redirect_is_refused(self) -> None:
        with self.assertRaises(reporting.Refused):
            reporting.NoRedirect().redirect_request(None)

    def test_destination_change_is_refused(self) -> None:
        self.response.geturl.return_value = "https://attacker.invalid/"
        with self.assertRaises(reporting.Refused):
            self.api("repos/barneyonline/ha-groq/actions/runs/42")

    def test_response_budget_is_enforced(self) -> None:
        self.response.read.return_value = b" " * 2_000_001
        with self.assertRaises(reporting.Refused):
            self.api("repos/barneyonline/ha-groq/actions/runs/42")


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "event.json").write_text("{}")
        self.environment = {
            "GITHUB_EVENT_NAME": "workflow_run",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "barneyonline/ha-groq",
            "GITHUB_REPOSITORY_ID": "1233471561",
            "GITHUB_EVENT_PATH": str(self.root / "event.json"),
            "GITHUB_OUTPUT": str(self.root / "outputs"),
            "RUNNER_TEMP": str(self.root),
            "REPORT_DIRECTORY": str(self.root / "reports"),
        }
        self.scope = {
            "eligible": True,
            "repository": "barneyonline/ha-groq",
            "run_id": 42,
            "run_attempt": 2,
            "artifact_id": 99,
            "head_sha": "a" * 40,
            "branch": "codex/issue-7",
            "pr_number": 72,
        }

    def run_main(self, operation: str) -> int:
        with patch.dict(os.environ, self.environment, clear=True), patch(
            "sys.argv", ["ci_reporting.py", operation]
        ), patch.object(reporting, "ReadAPI"), patch.object(
            reporting, "verify_scope", return_value=self.scope
        ):
            return reporting.main()

    def test_controller_and_scope_are_preserved_outside_reports(self) -> None:
        self.assertEqual(self.run_main("verify-event"), 0)
        self.assertEqual(
            (self.root / "hal-ci-reporting.py").read_bytes(),
            Path(reporting.__file__).read_bytes(),
        )
        sealed = json.loads((self.root / "hal-ci-scope.json").read_bytes())
        self.assertEqual(sealed["scope"], self.scope)
        self.assertIn("head_sha=" + "a" * 40, (self.root / "outputs").read_text())

    def test_existing_controller_is_not_overwritten(self) -> None:
        self.assertEqual(self.run_main("verify-event"), 0)
        raw = (self.root / "hal-ci-reporting.py").read_bytes()
        self.assertEqual(self.run_main("verify-event"), 1)
        self.assertEqual((self.root / "hal-ci-reporting.py").read_bytes(), raw)

    def test_revalidated_reports_are_required(self) -> None:
        self.assertEqual(self.run_main("verify-event"), 0)
        reports = self.root / "reports"
        reports.mkdir()
        (reports / "coverage.xml").write_text("<coverage/>")
        (reports / "junit.xml").write_text("<testsuites/>")
        self.assertEqual(self.run_main("validate-reports"), 0)
        (reports / "coverage.xml").unlink()
        self.assertEqual(self.run_main("validate-reports"), 1)

    def test_scope_change_blocks_upload_flags(self) -> None:
        self.assertEqual(self.run_main("verify-event"), 0)
        self.scope["head_sha"] = "b" * 40
        self.assertEqual(self.run_main("validate-reports"), 1)
        self.assertNotIn("coverage=true", (self.root / "outputs").read_text())

    def test_pr_context_is_not_a_trusted_controller(self) -> None:
        self.environment["GITHUB_EVENT_NAME"] = "pull_request"
        self.assertEqual(self.run_main("verify-event"), 1)
        self.assertFalse((self.root / "hal-ci-reporting.py").exists())

    def test_manual_rehearsal_refuses_non_numeric_run(self) -> None:
        self.environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        (self.root / "event.json").write_text(
            '{"inputs":{"producer_run_id":"42; touch marker"}}'
        )
        self.assertEqual(self.run_main("verify-event"), 1)
        self.assertFalse((self.root / "hal-ci-reporting.py").exists())


@unittest.skipIf(
    ROOT is None, "Standalone controller fixtures have no workflow definition"
)
class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = yaml.safe_load(
            (ROOT / ".github/workflows/coverage-reporting.yml").read_text()
        )
        self.job = self.workflow["jobs"]["upload"]

    def test_definition_is_default_branch_only_and_automatic_activation_is_off(
        self,
    ) -> None:
        events = self.workflow.get("on", self.workflow.get(True))
        self.assertEqual(set(events), {"workflow_run", "workflow_dispatch"})
        self.assertEqual(events["workflow_run"]["types"], ["completed"])
        self.assertEqual(set(events["workflow_run"]["workflows"]), {"Tests", "CI"})
        self.assertIn("github.ref == 'refs/heads/main'", self.job["if"])
        self.assertIn("vars.HAL_CI_REPORTING_ACTIVE == 'true'", self.job["if"])
        self.assertEqual(self.workflow["permissions"], {})

    def test_checkout_and_all_actions_are_fixed(self) -> None:
        for step in self.job["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[\w.-]+/[\w.-]+@[a-f0-9]{40}$")
                self.assertNotIn("actions/cache", step["uses"])
            if step.get("uses", "").startswith("actions/checkout@"):
                self.assertEqual(step["with"]["ref"], "${{ github.sha }}")
                self.assertIs(step["with"]["persist-credentials"], False)

    def test_upload_is_bound_to_verified_scope_and_data_only(self) -> None:
        self.assertNotIn("env", self.job)
        uploads = [
            s for s in self.job["steps"] if s.get("uses", "").startswith("codecov/")
        ]
        self.assertEqual(len(uploads), 2)
        for step in uploads:
            self.assertIn("steps.scope.outputs.eligible == 'true'", step["if"])
            data = step["with"]
            self.assertIs(data["disable_file_fixes"], True)
            self.assertIs(data["disable_search"], True)
            self.assertEqual(data["plugins"], "noop")
            self.assertEqual(
                data["override_commit"], "${{ steps.scope.outputs.head_sha }}"
            )
            self.assertEqual(data["slug"], "${{ github.repository }}")
            self.assertNotIn("binary", data)

    def test_artifact_reader_has_only_fixed_verified_identifiers(self) -> None:
        reader = next(
            s
            for s in self.job["steps"]
            if s.get("uses", "").startswith("actions/download-artifact@")
        )
        self.assertEqual(
            reader["with"]["artifact-ids"], "${{ steps.scope.outputs.artifact_id }}"
        )
        self.assertEqual(reader["with"]["run-id"], "${{ steps.scope.outputs.run_id }}")
        self.assertEqual(self.job["permissions"]["actions"], "read")
        self.assertEqual(self.job["permissions"]["contents"], "read")
        self.assertEqual(self.job["permissions"]["pull-requests"], "read")
        self.assertLessEqual(self.job["timeout-minutes"], 10)


if __name__ == "__main__":
    unittest.main()
