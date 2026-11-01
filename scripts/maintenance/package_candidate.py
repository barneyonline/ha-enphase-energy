#!/usr/bin/env python3
"""Build and inspect the exact component package before a release is published."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import runpy
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

COMPONENTS = {
    "ha_energy_planner": "ha-energy-planner-v{version}.zip",
    "enphase_ev": "enphase_ev-v{version}.zip",
    "groq": "groq-v{version}.zip",
}
CONTROLLER_ACTIONS = {
    ".github/workflows/maintenance-gate.yml": "merge-gate",
    ".github/workflows/maintenance-control.yml": "control",
    ".github/workflows/stale_blocked_issues.yml": "reporter-followup",
    ".github/workflows/coverage-reporting.yml": "coverage-reporting",
}

CALLER_TEMPLATE_SHA256 = {
    ".github/workflows/stale_blocked_issues.yml": "a817987414f6265b44aa86c1972cce96abb665a6c0dbff0efcaa9805f05cc1a5",
    ".github/workflows/coverage-reporting.yml": "0bdf414f23f4084e2020a2de8510f234547c2fb5eee35041560838105ca0dc6c",
    ".github/workflows/maintenance-control.yml": "acd5cb108642e568da2a0f41732fd281642848661a3138f8bb7ae60d6c0f34e6",
    ".github/workflows/maintenance-gate.yml": "713e663b82dbb75887b5733a862e03aaa43b52bedffdb0d369d5a8a3f1ea72d4",
}

CONTROL_FILES = (
    ".github/maintenance/merge-policy.json",
    ".github/maintenance/control-policy.json",
    ".github/workflows/maintenance-gate.yml",
    ".github/workflows/maintenance-review-events.yml",
    ".github/workflows/maintenance-control.yml",
    ".github/workflows/coverage-reporting.yml",
    ".github/workflows/release-candidate.yml",
    ".github/workflows/release-assets.yml",
    ".github/workflows/stale_blocked_issues.yml",
    "scripts/maintenance/package_candidate.py",
)


def verify_control_baseline(candidate: Path, trusted: Path) -> None:
    """Require approved maintenance controls even on a stable-derived patch branch."""
    for relative in CONTROL_FILES:
        source, baseline = candidate / relative, trusted / relative
        if (
            source.is_symlink()
            or baseline.is_symlink()
            or source.resolve() != candidate.resolve() / relative
            or baseline.resolve() != trusted.resolve() / relative
            or not source.is_file()
            or not baseline.is_file()
        ):
            raise ValueError(
                f"Missing regular maintenance-control baseline file: {relative}"
            )
        if source.read_bytes() != baseline.read_bytes():
            raise ValueError(
                f"Candidate requires the reviewed current maintenance-control baseline: {relative}"
            )

    pins = set()
    for relative, action in CONTROLLER_ACTIONS.items():
        raw = (candidate / relative).read_bytes()
        prefix = (
            b"barneyonline/hal-github-maintenance/actions/" + action.encode() + b"@"
        )
        matches = re.findall(re.escape(prefix) + rb"([0-9a-f]{40})", raw)
        if len(matches) != 1:
            raise ValueError(
                "Candidate requires one immutable central controller reference"
            )
        pins.add(matches[0])
        normalized = raw.replace(
            prefix + matches[0], prefix + b"__HAL_CONTROLLER_COMMIT__", 1
        )
        if hashlib.sha256(normalized).hexdigest() != CALLER_TEMPLATE_SHA256[relative]:
            raise ValueError(
                "Candidate caller differs from its reviewed canonical template"
            )
    if len(pins) != 1:
        raise ValueError(
            "Candidate requires one central controller version across all callers"
        )


def source_files(root: Path, domain: str) -> list[Path]:
    """List only regular component resources; symbolic links are never packaged."""
    component = root / "custom_components" / domain
    if component.is_symlink() or (root / "custom_components").is_symlink():
        raise ValueError("Component directory must not be a symbolic link")
    result = []
    for path in sorted(component.rglob("*")):
        parts = path.relative_to(component).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Component contains a symbolic link: {path.name}")
        if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
            result.append(path)
    return result


def inspect_archive(archive: Path, root: Path, domain: str, version: str) -> int:
    """Check the ZIP's CRC, paths, contents, Python syntax and JSON resources."""
    expected = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in source_files(root, domain)
    }
    required = {
        f"custom_components/{domain}/{name}"
        for name in (
            "manifest.json",
            "__init__.py",
            "config_flow.py",
            "strings.json",
            "translations/en.json",
        )
    }
    if not required.issubset(expected):
        raise ValueError("Component is missing required runtime resources")
    with ZipFile(archive) as zipped:
        entries = zipped.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ValueError("Archive contents do not match component source")
        if zipped.testzip() is not None:
            raise ValueError("Archive CRC validation failed")
        for entry in entries:
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if path.is_absolute() or ".." in path.parts or stat.S_ISLNK(mode):
                raise ValueError("Unsafe archive path or symbolic link")
            data = zipped.read(entry)
            if data != expected[entry.filename]:
                raise ValueError(
                    f"Archive resource differs from source: {entry.filename}"
                )
            if path.suffix == ".py":
                compile(data, entry.filename, "exec")
            elif path.suffix == ".json":
                json.loads(data)
        manifest = json.loads(zipped.read(f"custom_components/{domain}/manifest.json"))
        if manifest.get("domain") != domain or manifest.get("version") != version:
            raise ValueError("Packaged manifest domain or version does not match")
    return len(expected)


def package_candidate(
    root: Path, output: Path, domain: str, expected_version: str
) -> dict[str, Any]:
    """Build a deterministic archive while preserving each integration's filename."""
    if domain not in COMPONENTS:
        raise ValueError("Unknown component")
    version = expected_version.removeprefix("v")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]*", version):
        raise ValueError("Expected a safe release version")
    manifest = json.loads(
        (root / "custom_components" / domain / "manifest.json").read_text()
    )
    if manifest.get("version") != version or manifest.get("domain") != domain:
        raise ValueError("Candidate version and manifest domain/version must match")
    # Check links before invoking the Planner's established deterministic packager.
    files = source_files(root, domain)
    output.mkdir(parents=True, exist_ok=True)
    if domain == "ha_energy_planner":
        # The helper lives in trusted tooling, separately from candidate code.
        trusted_packager = Path(__file__).resolve().parents[1] / "package-release.py"
        packager = runpy.run_path(str(trusted_packager))["package_release"]
        archive = packager(root, output, expected_version)
    else:
        archive = output / COMPONENTS[domain].format(version=version)
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zipped:
            for path in files:
                entry = ZipInfo(
                    path.relative_to(root).as_posix(), date_time=(1980, 1, 1, 0, 0, 0)
                )
                entry.external_attr = 0o100644 << 16
                entry.compress_type = ZIP_DEFLATED
                zipped.writestr(entry, path.read_bytes())
    count = inspect_archive(archive, root, domain, version)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_suffix(".zip.sha256")
    checksum.write_text(f"{digest}  {archive.name}\n")
    result = {
        "domain": domain,
        "version": version,
        "archive": archive.name,
        "sha256": digest,
        "resource_count": count,
    }
    (output / "package-evidence.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--domain", choices=sorted(COMPONENTS), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument(
        "--historical-release-repair",
        action="store_true",
        help="Inspect an already-public release; this produces no pre-publication eligibility evidence",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_sha):
        parser.error("--expected-sha must be an exact 40-character commit SHA")
    actual = subprocess.run(
        ["git", "-C", str(args.root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual != args.expected_sha:
        raise SystemExit("Candidate checkout does not match its reviewed commit")
    dirty = subprocess.run(
        ["git", "-C", str(args.root), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if dirty.strip():
        raise SystemExit("Candidate checkout contains modifications or untracked files")
    if not args.historical_release_repair:
        verify_control_baseline(
            args.root.resolve(), Path(__file__).resolve().parents[2]
        )
    result = package_candidate(
        args.root.resolve(), args.output_dir.resolve(), args.domain, args.version
    )
    result["source_sha"] = actual
    result["maintenance_baseline_verified"] = not args.historical_release_repair
    (args.output_dir / "package-evidence.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
