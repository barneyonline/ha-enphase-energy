"""Validate actual package bytes and rejection of unsafe or mismatched candidates."""

from __future__ import annotations

import importlib.util
import io
import json
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "package_candidate", REPO / "scripts/maintenance/package_candidate.py"
)
assert SPEC and SPEC.loader
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


class CandidatePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def component(self, domain: str = "groq") -> Path:
        component = self.root / "custom_components" / domain
        (component / "translations").mkdir(parents=True)
        (component / "manifest.json").write_text(
            json.dumps({"domain": domain, "version": "1.2.3"})
        )
        (component / "__init__.py").write_text('"""Component."""\n')
        (component / "config_flow.py").write_text("VALUE = 1\n")
        (component / "strings.json").write_text("{}")
        (component / "translations/en.json").write_text("{}")
        return component

    def build(
        self, domain: str = "groq", version: str = "v1.2.3", output: str = "dist"
    ) -> dict[str, str]:
        return package.package_candidate(self.root, self.root / output, domain, version)

    def test_archives_are_byte_identical_and_keep_existing_filenames(self) -> None:
        for domain, filename in [
            ("groq", "groq-v1.2.3.zip"),
            ("enphase_ev", "enphase_ev-v1.2.3.zip"),
        ]:
            with self.subTest(domain=domain):
                self.component(domain)
                first = self.build(domain, output=f"first-{domain}")
                second = self.build(domain, output=f"second-{domain}")
                self.assertEqual(filename, first["archive"])
                self.assertEqual(first["sha256"], second["sha256"])
                self.assertEqual(
                    (self.root / f"first-{domain}" / filename).read_bytes(),
                    (self.root / f"second-{domain}" / filename).read_bytes(),
                )
                checksum = (
                    self.root / f"first-{domain}" / f"{filename}.sha256"
                ).read_text()
                self.assertEqual(f"{first['sha256']}  {filename}\n", checksum)

    def test_planner_preserves_its_packager_and_project_version_contract(self) -> None:
        component = self.component("ha_energy_planner")
        (component / "icons.json").write_text("{}")
        (self.root / "scripts").mkdir()
        # Use the real established Planner packager, not a matching fake implementation.
        planner_script = REPO / "scripts/package-release.py"
        if not planner_script.exists():
            planner_script = (
                REPO.parent / "ha-energy-planner/scripts/package-release.py"
            )
        if not planner_script.exists():
            self.skipTest("Planner's repository-specific packager is tested in Planner")
        shutil.copy2(planner_script, self.root / "scripts/package-release.py")
        (self.root / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
        result = self.build("ha_energy_planner")
        self.assertEqual("ha-energy-planner-v1.2.3.zip", result["archive"])
        (self.root / "pyproject.toml").write_text('[project]\nversion = "1.2.4"\n')
        with self.assertRaises(ValueError):
            self.build("ha_energy_planner")

    def test_excludes_caches_dotfiles_and_bytecode(self) -> None:
        component = self.component()
        (component / "__pycache__").mkdir()
        (component / "__pycache__/cache.pyc").write_bytes(b"cache")
        (component / ".env").write_text("not a package resource")
        (component / "old.pyc").write_bytes(b"cache")
        result = self.build()
        with ZipFile(self.root / "dist" / result["archive"]) as zipped:
            self.assertEqual(5, len(zipped.namelist()))

    def test_non_python_json_resources_are_preserved_exactly(self) -> None:
        component = self.component()
        resource = b"service:\n  name: Example service\n"
        (component / "services.yaml").write_bytes(resource)
        result = self.build()
        with ZipFile(self.root / "dist" / result["archive"]) as zipped:
            self.assertEqual(
                resource, zipped.read("custom_components/groq/services.yaml")
            )

    def test_planner_delegation_loads_only_trusted_tooling_and_validates_output(
        self,
    ) -> None:
        component = self.component("ha_energy_planner")
        output = self.root / "dist"
        output.mkdir()
        archive = output / "ha-energy-planner-v1.2.3.zip"
        with ZipFile(archive, "w") as zipped:
            for path in component.rglob("*"):
                if path.is_file():
                    zipped.write(path, path.relative_to(self.root).as_posix())
        candidate_script = self.root / "scripts/package-release.py"
        candidate_script.parent.mkdir()
        candidate_script.write_text(
            'raise AssertionError("Candidate tooling was executed")\n'
        )
        with patch.object(
            package.runpy,
            "run_path",
            return_value={"package_release": lambda root, target, version: archive},
        ) as load:
            result = self.build("ha_energy_planner")
        load.assert_called_once_with(
            str(Path(package.__file__).resolve().parents[1] / "package-release.py")
        )
        self.assertEqual(archive.name, result["archive"])

    def test_symlinks_are_rejected(self) -> None:
        component = self.component()
        (component / "linked.py").symlink_to(component / "__init__.py")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            self.build()

    def test_symlinked_component_directory_is_rejected(self) -> None:
        component = self.component()
        original = self.root / "original-component"
        component.rename(original)
        component.symlink_to(original, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            self.build()

    def test_manifest_version_domain_or_unsafe_version_is_rejected(self) -> None:
        component = self.component()
        with self.assertRaises(ValueError):
            self.build(version="v1.2.4")
        with self.assertRaises(ValueError):
            self.build(version="../../1.2.3")
        (component / "manifest.json").write_text(
            json.dumps({"domain": "wrong", "version": "1.2.3"})
        )
        with self.assertRaises(ValueError):
            self.build()
        with self.assertRaises(ValueError):
            self.build(domain="unknown")

    def test_missing_resources_invalid_json_and_syntax_are_rejected(self) -> None:
        component = self.component()
        (component / "strings.json").write_text("not json")
        with self.assertRaises(ValueError):
            self.build()
        (component / "strings.json").write_text("{}")
        (component / "config_flow.py").write_text("def broken(:\n")
        with self.assertRaises(SyntaxError):
            self.build()
        (component / "config_flow.py").unlink()
        with self.assertRaises(ValueError):
            self.build()

    def test_archive_tampering_is_rejected(self) -> None:
        self.component()
        result = self.build()
        archive = self.root / "dist" / result["archive"]
        with ZipFile(archive, "a") as zipped:
            zipped.writestr("../../outside.py", "pass")
        with self.assertRaises(ValueError):
            package.inspect_archive(archive, self.root, "groq", "1.2.3")

    def test_crc_corruption_is_rejected(self) -> None:
        self.component()
        result = self.build()
        archive = self.root / "dist" / result["archive"]
        data = bytearray(archive.read_bytes())
        central = data.index(b"PK\x01\x02")
        data[central + 16] ^= 1
        archive.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "CRC"):
            package.inspect_archive(archive, self.root, "groq", "1.2.3")

    def test_resource_changes_or_symlink_entries_are_rejected(self) -> None:
        self.component()
        result = self.build()
        archive = self.root / "dist" / result["archive"]
        with ZipFile(archive) as zipped:
            contents = {name: zipped.read(name) for name in zipped.namelist()}
        for tamper in ["content", "symlink"]:
            with self.subTest(tamper=tamper):
                with ZipFile(archive, "w") as zipped:
                    for index, (name, data) in enumerate(contents.items()):
                        entry = ZipInfo(name)
                        if index == 0:
                            if tamper == "content":
                                data += b"\n"
                            else:
                                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                        zipped.writestr(entry, data)
                with self.assertRaises(ValueError):
                    package.inspect_archive(archive, self.root, "groq", "1.2.3")

    def test_packaged_version_is_verified_against_requested_version(self) -> None:
        self.component()
        result = self.build()
        with self.assertRaisesRegex(ValueError, "Packaged manifest"):
            package.inspect_archive(
                self.root / "dist" / result["archive"], self.root, "groq", "1.2.4"
            )

    def test_stable_derived_candidates_require_current_control_baseline(self) -> None:
        candidate, trusted = self.root / "candidate", self.root / "trusted"
        for relative in package.CONTROL_FILES:
            current = REPO / relative
            self.assertTrue(current.is_file(), f"Missing current control: {relative}")
            for directory in [candidate, trusted]:
                path = directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current, path)
        package.verify_control_baseline(candidate, trusted)
        (candidate / package.CONTROL_FILES[0]).write_text("old controls")
        with self.assertRaisesRegex(ValueError, "current maintenance-control baseline"):
            package.verify_control_baseline(candidate, trusted)
        (candidate / package.CONTROL_FILES[0]).unlink()
        with self.assertRaisesRegex(
            ValueError, "Missing regular maintenance-control baseline"
        ):
            package.verify_control_baseline(candidate, trusted)

    def prepare_git(self) -> str:
        self.component()
        subprocess.run(
            ["git", "init", "-q", str(self.root)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(self.root), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "Fixture",
            ],
            check=True,
            capture_output=True,
        )
        return subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def invoke(self, sha: str, historical: bool = False) -> dict[str, str]:
        output_directory = tempfile.TemporaryDirectory()
        self.addCleanup(output_directory.cleanup)
        args = [
            "package_candidate.py",
            "--root",
            str(self.root),
            "--output-dir",
            output_directory.name,
            "--domain",
            "groq",
            "--version",
            "v1.2.3",
            "--expected-sha",
            sha,
        ]
        if historical:
            args.append("--historical-release-repair")
        output = io.StringIO()
        with patch.object(sys, "argv", args), redirect_stdout(output):
            package.main()
        return json.loads(output.getvalue())

    def test_command_binds_provenance_to_clean_git_commit_and_baseline(self) -> None:
        sha = self.prepare_git()
        with patch.object(package, "verify_control_baseline") as baseline:
            result = self.invoke(sha)
        self.assertEqual(sha, result["source_sha"])
        self.assertTrue(result["maintenance_baseline_verified"])
        baseline.assert_called_once()

    def test_historical_repair_does_not_claim_prepublication_eligibility(self) -> None:
        sha = self.prepare_git()
        with patch.object(package, "verify_control_baseline") as baseline:
            result = self.invoke(sha, historical=True)
        self.assertFalse(result["maintenance_baseline_verified"])
        baseline.assert_not_called()

    def test_wrong_sha_and_untracked_component_files_are_rejected(self) -> None:
        sha = self.prepare_git()
        with self.assertRaisesRegex(SystemExit, "reviewed commit"):
            self.invoke("0" * 40)
        (self.root / "custom_components/groq/untracked.py").write_text(
            "UNREVIEWED = True\n"
        )
        with self.assertRaisesRegex(SystemExit, "untracked files"):
            self.invoke(sha)

    def test_invalid_sha_and_script_entrypoint_fail_without_building(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke("main")
        with (
            patch.object(sys, "argv", ["package_candidate.py"]),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            runpy.run_path(
                str(REPO / "scripts/maintenance/package_candidate.py"),
                run_name="__main__",
            )


if __name__ == "__main__":
    unittest.main()
