"""Bounded market-feed lag and reconnect telemetry."""

from __future__ import annotations

import collections
from collections.abc import Mapping


def event_time_s(event: Mapping[str, object]) -> float | None:
    try:
        timestamp = float(str(event["timestamp"]))
    except (KeyError, TypeError, ValueError):
        return None
    return timestamp / 1000 if timestamp >= 100_000_000_000 else timestamp


class FeedHealth:
    def __init__(self, sample_size: int = 4096) -> None:
        self.lag_ms: collections.deque[float] = collections.deque(maxlen=sample_size)
        self.reconnects = 0

    def observe(self, event: Mapping[str, object], received_at: float) -> None:
        event_at = event_time_s(event)
        if event_at is None:
            return
        lag = (received_at - event_at) * 1000
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
