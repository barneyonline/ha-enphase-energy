"""Test the Home Assistant-dependent snapshot benchmark with integration fixtures."""

import importlib.util
from pathlib import Path
import runpy

import pytest


def _module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "benchmark_snapshots.py"
    spec = importlib.util.spec_from_file_location("benchmark_snapshots", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_benchmark_shape_and_scope():
    module = _module()
    payload = module.make_payload(1)
    baseline = module.legacy_freeze(payload)
    current = module.freeze_charger_data(payload)
    expected = dict(baseline["synthetic-0"])
    expected.pop("fetched_at_utc")
    assert dict(current["synthetic-0"]) == expected
    payload["synthetic-0"]["schedule"].clear()
    assert len(current["synthetic-0"]["schedule"]) == 4
    result = module.benchmark(iterations=1)
    assert [case["chargers"] for case in result["scenarios"]] == [1, 8, 32]
    for case in result["scenarios"]:
        for metrics in case["results"].values():
            assert metrics["median_microseconds_per_build"] > 0
            assert metrics["peak_traced_bytes"] > 0
    assert [case["inverters"] for case in result["mixed_inventory_scenarios"]] == [
        50,
        500,
    ]
    for case in result["mixed_inventory_scenarios"]:
        assert case["batteries"] == 20
        current = case["results"]["unchanged"]
        assert current["reused_inventory"] is True
        assert current["reused_battery"] is True
        assert (
            current["peak_traced_bytes"] < case["results"]["fresh"]["peak_traced_bytes"]
        )
    with pytest.raises(ValueError, match="positive"):
        module.benchmark(iterations=0)


def test_benchmark_cli(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["benchmark_snapshots", "--iterations", "1"])
    path = Path(__file__).resolve().parents[3] / "scripts" / "benchmark_snapshots.py"
    runpy.run_path(str(path), run_name="__main__")
    assert '"iterations_per_batch": 1' in capsys.readouterr().out
