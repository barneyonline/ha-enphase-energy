"""Compare snapshot construction with the prior redundant freeze traversal.

Run in ha-dev with PYTHONPATH=. Synthetic payloads measure Python work only;
they do not represent measured Enphase cloud latency or live HA recorder load.
"""

from __future__ import annotations

import argparse
import json
import platform
from statistics import median
from timeit import repeat
import tracemalloc

from custom_components.enphase_ev.integration_snapshot import freeze_charger_data
from custom_components.enphase_ev.snapshot_helpers import freeze_snapshot_mapping


def make_payload(count: int) -> dict[str, dict[str, object]]:
    """Generate deterministic nested charger telemetry, without identifiers."""

    return {
        f"synthetic-{index}": {
            "charging": bool(index % 2),
            "plugged": True,
            "charge_mode": "MANUAL_CHARGING",
            "lifetime_kwh": 1000.0 + index,
            "sampled_at_utc": "2026-09-05T00:00:00+00:00",
            "fetched_at_utc": "2026-09-05T00:00:01+00:00",
            "electrical": {"phases": [{"voltage": 230, "amps": 16}] * 3},
            "schedule": [
                {"start": 60 * slot, "end": 60 * (slot + 1), "days": list(range(7))}
                for slot in range(4)
            ],
        }
        for index in range(count)
    }


def legacy_freeze(data: dict[str, dict[str, object]]) -> object:
    """Reproduce the two recursive traversals in the reviewed baseline."""

    return freeze_snapshot_mapping(
        {serial: freeze_snapshot_mapping(payload) for serial, payload in data.items()}
    )


def benchmark(*, iterations: int = 100) -> dict[str, object]:
    """Report timing and peak traced allocation for fixed inventory sizes."""

    if iterations < 1:
        raise ValueError("iterations must be positive")
    scenarios = []
    for count in (1, 8, 32):
        data = make_payload(count)
        results = {}
        for label, builder in (
            ("baseline", legacy_freeze),
            ("current", freeze_charger_data),
        ):
            builder(data)
            batches = repeat(lambda: builder(data), number=iterations, repeat=5)
            tracemalloc.start()
            snapshot = builder(data)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert len(snapshot) == count
            results[label] = {
                "median_microseconds_per_build": round(
                    median(batches) * 1e6 / iterations, 3
                ),
                "peak_traced_bytes": peak,
            }
        scenarios.append({"chargers": count, "results": results})
    return {
        "python": platform.python_version(),
        "iterations_per_batch": iterations,
        "batches": 5,
        "scope": "synthetic nested charger freeze; excludes network and HA recorder",
        "scenarios": scenarios,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(benchmark(iterations=args.iterations), indent=2))


if __name__ == "__main__":
    main()
