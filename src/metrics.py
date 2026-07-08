"""Structured metrics — independent from logging.

Why separate from logging:
- Logging is human-readable, one event at a time.
- Metrics aggregate, support counters/timers, and pivot into Prometheus / OTel
  later without churn. Keeping them out of `logging` prevents log formatting
  from leaking into the metrics surface.

Design:
- All state lives on one `MetricsCollector`. In-process by default; the API
  is small enough that a Prometheus exporter or OTel meter can wrap it
  without touching call sites.
- Timings are recorded with a `timer(...)` context manager so phase
  boundaries stay readable at the call site.
"""
from __future__ import annotations

import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class MetricsSnapshot:
    """Immutable view of collector state at one instant."""

    counters: dict[str, int]
    timings_ms: dict[str, tuple[float, ...]]


class MetricsCollector:
    """In-process metrics store."""

    def __init__(self) -> None:
        self._counters: Counter[str] = Counter()
        self._timings_ms: dict[str, list[float]] = {}

    def increment(self, name: str, *, value: int = 1) -> None:
        self._counters[name] += value

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._timings_ms.setdefault(name, []).append(elapsed_ms)

    def snapshot(self) -> MetricsSnapshot:
        # ponytail: copy then freeze tuples so callers can hand it to exporters safely.
        return MetricsSnapshot(
            counters=dict(self._counters),
            timings_ms={k: tuple(v) for k, v in self._timings_ms.items()},
        )

    # Convenience for tests / log lines; keeps log behavior unchanged when called.
    def format_summary(self) -> str:
        snap = self.snapshot()
        parts: list[str] = []
        if snap.counters:
            parts.append("counters: " + ", ".join(f"{k}={v}" for k, v in sorted(snap.counters.items())))
        if snap.timings_ms:
            parts.append("timings_ms: " + ", ".join(
                f"{k}={sum(v)/len(v):.1f} (n={len(v)})" for k, v in sorted(snap.timings_ms.items())
            ))
        return "; ".join(parts) if parts else "<no metrics>"
