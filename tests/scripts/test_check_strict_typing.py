"""Tests for the full-package strict typing debt check."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


def _load_module():
    root = Path(__file__).resolve().parents[2]
    module_path = root / "scripts" / "check_strict_typing.py"
    spec = importlib.util.spec_from_file_location("check_strict_typing", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_strict_typing = _load_module()


def _runner(returncode: int, output: str):
    def run(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout=output, stderr="")

    return run


def test_count_mypy_errors_only_counts_error_diagnostics() -> None:
    output = "\n".join(
        (
            "module.py:1: error: Missing return type [no-untyped-def]",
            "module.py:1: note: Add -> None",
            "Found 1 error in 1 file",
        )
    )

    assert check_strict_typing.count_mypy_errors(output) == 1


def test_typing_debt_at_baseline_passes(tmp_path: Path, capsys) -> None:
    output = "\n".join(f"module.py:{line}: error: typing debt" for line in range(2))

    result = check_strict_typing.check_strict_typing_progress(
        root=tmp_path,
        max_errors=2,
        runner=_runner(1, output),
    )

    assert result == 0
    assert "2 errors (baseline 2)" in capsys.readouterr().out


def test_typing_debt_regression_fails(tmp_path: Path, capsys) -> None:
    output = "\n".join(f"module.py:{line}: error: typing debt" for line in range(3))

    result = check_strict_typing.check_strict_typing_progress(
        root=tmp_path,
        max_errors=2,
        runner=_runner(1, output),
    )

    assert result == 1
    assert "3 errors exceeds the baseline of 2" in capsys.readouterr().out


def test_clean_strict_mypy_passes(tmp_path: Path, capsys) -> None:
    result = check_strict_typing.check_strict_typing_progress(
        root=tmp_path,
        max_errors=2,
        runner=_runner(0, "Success: no issues found"),
    )

    assert result == 0
    assert "passes for the full integration package" in capsys.readouterr().out


def test_unparseable_mypy_failure_is_reported(tmp_path: Path, capsys) -> None:
    result = check_strict_typing.check_strict_typing_progress(
        root=tmp_path,
        max_errors=2,
        runner=_runner(1, "mypy failed without a diagnostic"),
    )

    assert result == 1
    assert "without reporting parseable" in capsys.readouterr().out


def test_unexpected_mypy_failure_is_reported(tmp_path: Path, capsys) -> None:
    result = check_strict_typing.check_strict_typing_progress(
        root=tmp_path,
        max_errors=2,
        runner=_runner(2, "mypy failed to start"),
    )

    assert result == 2
    assert "could not run successfully" in capsys.readouterr().out


def test_main_runs_progress_check(monkeypatch) -> None:
    calls: list[tuple[Path, int]] = []

    def _check(*, root: Path, max_errors: int) -> int:
        calls.append((root, max_errors))
        return 0

    monkeypatch.setattr(check_strict_typing, "check_strict_typing_progress", _check)

    assert check_strict_typing.main(["--max-errors", "1874"]) == 0
    assert calls == [(Path(check_strict_typing.__file__).resolve().parents[1], 1874)]


def test_main_rejects_negative_baseline() -> None:
    with pytest.raises(SystemExit, match="2"):
        check_strict_typing.main(["--max-errors", "-1"])
