"""Refresh performance diagnostics helpers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from math import ceil

REFRESH_TOTAL_BUDGET_S = 30.0
REFRESH_STAGE_BUDGET_S = 5.0
REFRESH_PERFORMANCE_HISTORY_LIMIT = 50


def _clean_phase_timings(phase_timings: dict[str, float]) -> dict[str, float]:
    timings: dict[str, float] = {}
    for key, value in (phase_timings or {}).items():
        try:
            timing = float(value)
        except (TypeError, ValueError):
            continue
        if timing < 0:
            continue
        timings[str(key)] = round(timing, 3)
    return timings


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = max(1, ceil((percentile / 100.0) * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 3)


def _numeric_summary(values: Iterable[float]) -> dict[str, object]:
    ordered = sorted(float(value) for value in values)
    return {
        "count": len(ordered),
        "p50_s": _percentile(ordered, 50),
        "p95_s": _percentile(ordered, 95),
        "max_s": round(ordered[-1], 3) if ordered else None,
    }


def refresh_performance_summary(
    phase_timings: dict[str, float],
    *,
    latency_ms: int | None = None,
    cloud_calls: int | None = None,
    steady_cloud_calls: int | None = None,
    fast_cloud_calls: int | None = None,
    total_budget_s: float = REFRESH_TOTAL_BUDGET_S,
    stage_budget_s: float = REFRESH_STAGE_BUDGET_S,
) -> dict[str, object]:
    """Return a compact refresh timing budget summary."""

    timings = _clean_phase_timings(phase_timings)
    stage_timings = {key: value for key, value in timings.items() if key != "total_s"}
    total_s = timings.get("total_s")
    if total_s is None and latency_ms is not None:
        try:
            total_s = round(float(latency_ms) / 1000.0, 3)
        except (TypeError, ValueError):
            total_s = None

    slowest_stage = None
    slowest_stage_s = None
    if stage_timings:
        slowest_stage, slowest_stage_s = max(
            stage_timings.items(), key=lambda item: item[1]
        )

    over_budget_stages = sorted(
        key for key, value in stage_timings.items() if value > stage_budget_s
    )

    return {
        "total_s": total_s,
        "total_budget_s": total_budget_s,
        "total_over_budget": (
            bool(total_s > total_budget_s) if total_s is not None else False
        ),
        "timed_stage_count": len(stage_timings),
        "stage_budget_s": stage_budget_s,
        "over_budget_stages": over_budget_stages,
        "slowest_stage": slowest_stage,
        "slowest_stage_s": slowest_stage_s,
        "cloud_calls": cloud_calls,
        "steady_cloud_calls": steady_cloud_calls,
        "fast_cloud_calls": fast_cloud_calls,
    }


def record_refresh_performance_sample(
    history: list[dict[str, object]],
    phase_timings: dict[str, float],
    *,
    refresh_started_utc: datetime | None = None,
    latency_ms: int | None = None,
    cloud_calls: int | None = None,
    fast_poll: bool = False,
    first_refresh: bool = False,
    payload_using_stale: bool = False,
    manual_bypass: bool = False,
    outcome: str = "success",
    max_samples: int = REFRESH_PERFORMANCE_HISTORY_LIMIT,
) -> list[dict[str, object]]:
    """Append a diagnostics-safe refresh sample and return the trimmed history."""

    timings = _clean_phase_timings(phase_timings)
    if not timings and latency_ms is None:
        return list(history or [])[-max_samples:]

    summary = refresh_performance_summary(
        timings,
        latency_ms=latency_ms,
        cloud_calls=cloud_calls,
    )
    sample: dict[str, object] = {
        "phase_timings": timings,
        "total_s": summary["total_s"],
        "slowest_stage": summary["slowest_stage"],
        "slowest_stage_s": summary["slowest_stage_s"],
        "cloud_calls": cloud_calls,
        "fast_poll": bool(fast_poll),
        "first_refresh": bool(first_refresh),
        "payload_using_stale": bool(payload_using_stale),
        "manual_bypass": bool(manual_bypass),
        "outcome": str(outcome),
    }
    if refresh_started_utc is not None:
        try:
            sample["started_utc"] = refresh_started_utc.isoformat()
        except Exception:
            sample["started_utc"] = str(refresh_started_utc)

    samples = [dict(item) for item in (history or []) if isinstance(item, dict)]
    samples.append(sample)
    limit = max(1, int(max_samples or REFRESH_PERFORMANCE_HISTORY_LIMIT))
    return samples[-limit:]


def refresh_performance_history_summary(
    history: list[dict[str, object]],
) -> dict[str, object]:
    """Summarize rolling refresh samples for diagnostics."""

    samples = [sample for sample in (history or []) if isinstance(sample, dict)]
    total_values: list[float] = []
    cloud_call_values: list[float] = []
    stage_values: dict[str, list[float]] = {}
    fast_count = 0
    first_refresh_count = 0
    stale_count = 0
    manual_bypass_count = 0
    failed_count = 0
    cancelled_count = 0
    for sample in samples:
        if sample.get("fast_poll"):
            fast_count += 1
        if sample.get("first_refresh"):
            first_refresh_count += 1
        if sample.get("payload_using_stale"):
            stale_count += 1
        if sample.get("manual_bypass"):
            manual_bypass_count += 1
        if sample.get("outcome") == "failed":
            failed_count += 1
        elif sample.get("outcome") == "cancelled":
            cancelled_count += 1
        try:
            total = float(str(sample["total_s"]))
        except (KeyError, TypeError, ValueError):
            total = None
        if total is not None and total >= 0:
            total_values.append(total)
        try:
            cloud_calls = float(str(sample["cloud_calls"]))
        except (KeyError, TypeError, ValueError):
            cloud_calls = None
        if cloud_calls is not None and cloud_calls >= 0:
            cloud_call_values.append(cloud_calls)
        phase_timings = sample.get("phase_timings")
        if not isinstance(phase_timings, dict):
            continue
        for key, value in _clean_phase_timings(phase_timings).items():
            if key == "total_s":
                continue
            stage_values.setdefault(key, []).append(value)

    cloud_summary = _numeric_summary(cloud_call_values)
    return {
        "sample_count": len(samples),
        "window_size": REFRESH_PERFORMANCE_HISTORY_LIMIT,
        "fast_poll_count": fast_count,
        "steady_poll_count": max(0, len(samples) - fast_count),
        "first_refresh_count": first_refresh_count,
        "payload_using_stale_count": stale_count,
        "manual_bypass_count": manual_bypass_count,
        "failed_count": failed_count,
        "cancelled_count": cancelled_count,
        "total_s": _numeric_summary(total_values),
        "cloud_calls": {
            "count": cloud_summary["count"],
            "p50": cloud_summary["p50_s"],
            "p95": cloud_summary["p95_s"],
            "max": cloud_summary["max_s"],
        },
        "stages": {
            key: _numeric_summary(values)
            for key, values in sorted(stage_values.items())
        },
        "latest": dict(samples[-1]) if samples else None,
    }
