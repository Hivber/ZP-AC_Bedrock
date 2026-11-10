"""Low-overhead in-memory health counters; no player identifiers are collected."""
from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Any


class RuntimeMetrics:
    def __init__(self, sample_window: int = 120) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = defaultdict(int)
        self._durations: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=max(10, sample_window)))

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[str(name)[:64]] += max(0, int(amount))

    def observe_ms(self, name: str, value: float) -> None:
        try:
            sample = max(0.0, min(60000.0, float(value)))
        except (TypeError, ValueError, OverflowError):
            return
        with self._lock:
            self._durations[str(name)[:64]].append(sample)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            durations = {}
            for name, values in self._durations.items():
                ordered = sorted(values)
                if not ordered:
                    continue
                durations[name] = {
                    "samples": len(ordered),
                    "last_ms": round(values[-1], 3),
                    "p95_ms": round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))], 3),
                    "max_ms": round(ordered[-1], 3),
                }
            return {"counters": dict(self._counts), "durations": durations}
