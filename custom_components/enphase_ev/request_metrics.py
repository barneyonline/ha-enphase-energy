"""Operation-scoped cloud request performance metrics."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class RequestMetrics:
    """Mutable request metrics for one logical integration operation."""

    operation: str
    attempts: int = 0
    queue_s: float = 0.0
    network_s: float = 0.0
    parsing_s: float = 0.0

    def phase_timings(self) -> dict[str, float]:
        """Return non-zero request-layer timing totals for diagnostics."""

        timings: dict[str, float] = {}
        for key, value in (
            ("request_queue_s", self.queue_s),
            ("request_network_s", self.network_s),
            ("response_parsing_s", self.parsing_s),
        ):
            if value > 0:
                timings[key] = round(value, 3)
        return timings


_CURRENT_REQUEST_METRICS: ContextVar[RequestMetrics | None] = ContextVar(
    "enphase_ev_request_metrics",
    default=None,
)


@contextmanager
def request_metrics_scope(operation: str) -> Iterator[RequestMetrics]:
    """Collect cloud request metrics for a logical operation."""

    metrics = RequestMetrics(operation=str(operation))
    token = _CURRENT_REQUEST_METRICS.set(metrics)
    try:
        yield metrics
    finally:
        _CURRENT_REQUEST_METRICS.reset(token)


def record_request_attempt() -> None:
    """Record one HTTP attempt in the active operation, when present."""

    metrics = _CURRENT_REQUEST_METRICS.get()
    if metrics is not None:
        metrics.attempts += 1


def record_request_timings(
    *,
    queue_s: float = 0.0,
    network_s: float = 0.0,
    parsing_s: float = 0.0,
) -> None:
    """Add request-layer timings to the active operation, when present."""

    metrics = _CURRENT_REQUEST_METRICS.get()
    if metrics is None:
        return
    metrics.queue_s += max(0.0, float(queue_s))
    metrics.network_s += max(0.0, float(network_s))
    metrics.parsing_s += max(0.0, float(parsing_s))
