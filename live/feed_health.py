"""Bounded market-feed lag and reconnect telemetry."""

from __future__ import annotations

import collections
from collections.abc import Mapping

MARKET_WS_MAX_QUEUE = 4096


def event_time_s(event: Mapping[str, object]) -> float | None:
    try:
        timestamp = float(str(event["timestamp"]))
    except (KeyError, TypeError, ValueError):
        return None
    return timestamp / 1000 if timestamp >= 100_000_000_000 else timestamp


def stale_market_event(event: Mapping[str, object], received_at: float,
                       max_lag_s: float) -> bool:
    """Fail closed when a causal book update is missing or arrives too late."""
    if event.get("event_type") not in ("book", "price_change"):
        return False
    event_at = event_time_s(event)
    return event_at is None or received_at - event_at > max_lag_s


def market_event_tokens(event: Mapping[str, object]) -> set[str]:
    """Return token ids touched by a book snapshot or price-level update."""
    tokens: set[str] = set()
    token = str(event.get("asset_id") or "")
    if token:
        tokens.add(token)
    rows = event.get("price_changes") or []
    if isinstance(rows, list):
        tokens.update(
            str(row.get("asset_id") or "")
            for row in rows if isinstance(row, dict) and row.get("asset_id")
        )
    return tokens


class FeedHealth:
    def __init__(self, sample_size: int = 4096) -> None:
        self.lag_ms: collections.deque[float] = collections.deque(maxlen=sample_size)
        self.reconnects = 0
        self.missing_timestamps = 0
        self.out_of_range_timestamps = 0
        self.interval_max_ms = 0.0
        self.lifetime_max_ms = 0.0

    def observe(self, event: Mapping[str, object], received_at: float) -> None:
        event_at = event_time_s(event)
        if event_at is None:
            self.missing_timestamps += 1
            return
        lag = (received_at - event_at) * 1000
        if -1000 <= lag <= 60_000:
            lag = max(0.0, lag)
            self.lag_ms.append(lag)
            self.interval_max_ms = max(self.interval_max_ms, lag)
            self.lifetime_max_ms = max(self.lifetime_max_ms, lag)
        else:
            self.out_of_range_timestamps += 1

    def reconnect(self) -> None:
        self.reconnects += 1

    def snapshot(self, *, reset_interval: bool = False) -> dict[str, int]:
        values = sorted(self.lag_ms)
        snapshot = {
            "p50_ms": round(values[(len(values) - 1) // 2]) if values else 0,
            "p90_ms": round(values[int((len(values) - 1) * 0.9)]) if values else 0,
            "max_ms": round(values[-1]) if values else 0,
            "interval_max_ms": round(self.interval_max_ms),
            "lifetime_max_ms": round(self.lifetime_max_ms),
            "missing_timestamps": self.missing_timestamps,
            "out_of_range_timestamps": self.out_of_range_timestamps,
            "reconnects": self.reconnects,
        }
        if reset_interval:
            self.interval_max_ms = 0.0
        return snapshot
