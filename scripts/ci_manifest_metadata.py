"""Identify manifest edits that do not require integration runtime tests."""

from __future__ import annotations

import argparse
import json
import subprocess

MANIFEST = "custom_components/enphase_ev/manifest.json"
# Keep this list narrow: new and runtime-affecting keys require the full suite.
METADATA_KEYS = frozenset(
    {"categories", "codeowners", "documentation", "issue_tracker"}
)


def metadata_only(base_ref: str) -> bool:
    """Compare committed manifests at the PR merge base and checked-out head."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True
        ).stdout

    try:
        base = git("merge-base", base_ref, "HEAD").strip()
        before = json.loads(git("show", f"{base}:{MANIFEST}"))
        after = json.loads(git("show", f"HEAD:{MANIFEST}"))
    except (subprocess.CalledProcessError, ValueError):
        # Missing, invalid, added, or deleted manifests keep runtime validation.
        return False
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    # JSON comparison preserves types (Python otherwise considers True == 1).
    return json.dumps(
        {key: value for key, value in before.items() if key not in METADATA_KEYS},
        sort_keys=True,
    ) == json.dumps(
        {key: value for key, value in after.items() if key not in METADATA_KEYS},
        sort_keys=True,
    )


def main() -> int:
    """Return success only when the manifest is safe to exclude from pytest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    args = parser.parse_args()
    return 0 if metadata_only(args.base_ref) else 1


if __name__ == "__main__":
    raise SystemExit(main())
