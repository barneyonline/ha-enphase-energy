#!/usr/bin/env python3
"""Track full-package strict mypy debt without claiming strict typing."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
import subprocess

MYPY_COMMAND = (
    "mypy",
    "--strict",
    "--ignore-missing-imports",
    "--follow-imports=skip",
    "custom_components/enphase_ev",
)

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def count_mypy_errors(output: str) -> int:
    """Return the number of mypy error diagnostics in output."""

    return sum(" error: " in line for line in output.splitlines())


def check_strict_typing_progress(
    *,
    root: Path,
    max_errors: int,
    runner: RunCommand = subprocess.run,
) -> int:
    """Run strict mypy and fail when package typing debt increases."""

    result = runner(
        MYPY_COMMAND,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    error_count = count_mypy_errors(output)

    if result.returncode not in (0, 1):
        print(output)
        print(f"Strict mypy could not run successfully (exit {result.returncode}).")
        return result.returncode

    if result.returncode == 1 and error_count == 0:
        print(output)
        print("Strict mypy failed without reporting parseable error diagnostics.")
        return 1

    if error_count > max_errors:
        print(output)
        print(
            "Strict typing debt increased: "
            f"{error_count} errors exceeds the baseline of {max_errors}."
        )
        return 1

    if error_count == 0:
        print("Strict mypy passes for the full integration package.")
        return 0

    print(
        "Strict typing debt did not increase: "
        f"{error_count} errors (baseline {max_errors})."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line strict typing progress check."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-errors",
        type=int,
        required=True,
        help="Fail when strict mypy reports more than this many errors.",
    )
    args = parser.parse_args(argv)
    if args.max_errors < 0:
        parser.error("--max-errors must be zero or greater")
    return check_strict_typing_progress(
        root=Path(__file__).resolve().parents[1],
        max_errors=args.max_errors,
    )


if __name__ == "__main__":  # pragma: no cover - exercised by the CI command
    raise SystemExit(main())
