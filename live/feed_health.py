"""Bounded market-feed lag and reconnect telemetry."""

from __future__ import annotations

import collections
from collections.abc import Mapping


class FeedHealth:
    def __init__(self, sample_size: int = 4096) -> None:
        self.lag_ms: collections.deque[float] = collections.deque(maxlen=sample_size)
        self.reconnects = 0

    def observe(self, event: Mapping[str, object], received_at: float) -> None:
        try:
            timestamp = float(str(event["timestamp"]))
        except (KeyError, TypeError, ValueError):
            return
        if timestamp < 100_000_000_000:
            timestamp *= 1000
        lag = received_at * 1000 - timestamp
        if -1000 <= lag <= 60_000:
            self.lag_ms.append(max(0.0, lag))

    def reconnect(self) -> None:
        self.reconnects += 1

    def snapshot(self) -> dict[str, int]:
        values = sorted(self.lag_ms)
        if not values:
            return {"p50_ms": 0, "p90_ms": 0, "max_ms": 0,
                    "reconnects": self.reconnects}
        return {
            "p50_ms": round(values[(len(values) - 1) // 2]),
            "p90_ms": round(values[int((len(values) - 1) * 0.9)]),
            "max_ms": round(values[-1]),
            "reconnects": self.reconnects,
        }
