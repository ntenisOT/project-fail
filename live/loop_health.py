"""Low-overhead event-loop scheduling-lag telemetry."""

from __future__ import annotations

import asyncio
import collections
import time


class EventLoopHealth:
    def __init__(self, sample_size: int = 4096) -> None:
        self.lag_ms: collections.deque[float] = collections.deque(maxlen=sample_size)
        self.interval_max_ms = 0.0
        self.lifetime_max_ms = 0.0

    def observe(self, lag_ms: float) -> None:
        lag_ms = max(0.0, lag_ms)
        self.lag_ms.append(lag_ms)
        self.interval_max_ms = max(self.interval_max_ms, lag_ms)
        self.lifetime_max_ms = max(self.lifetime_max_ms, lag_ms)

    async def run(self, period_s: float = 0.05) -> None:
        while True:
            expected = time.monotonic() + period_s
            await asyncio.sleep(period_s)
            self.observe((time.monotonic() - expected) * 1000)

    def snapshot(self, *, reset_interval: bool = False) -> dict[str, int]:
        values = sorted(self.lag_ms)
        snapshot = {
            "p50_ms": round(values[(len(values) - 1) // 2]) if values else 0,
            "p90_ms": round(values[int((len(values) - 1) * 0.9)]) if values else 0,
            "max_ms": round(values[-1]) if values else 0,
            "interval_max_ms": round(self.interval_max_ms),
            "lifetime_max_ms": round(self.lifetime_max_ms),
        }
        if reset_interval:
            self.interval_max_ms = 0.0
        return snapshot
