"""Classify settled paper windows by measured feed-tail exposure."""

from __future__ import annotations

import dataclasses
import json
import sqlite3


@dataclasses.dataclass(frozen=True)
class FeedQualitySnapshot:
    strategy: str
    quality: str
    windows: int
    pnl: float
    neutral_pnl: float
    worst_pnl: float
    unmatched: float
    max_lag_ms: float


def snapshots(db: sqlite3.Connection) -> list[FeedQualitySnapshot]:
    quality_by_window: dict[tuple[str, str], tuple[str, float]] = {}
    for strategy, slug, raw in db.execute(
        "SELECT strategy,slug,data FROM window_metrics"
    ):
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(values, dict) or "exposed_stale_market_events" not in values:
            continue
        try:
            lagged = (
                float(values.get("exposed_stale_market_events", 0))
                + float(values.get("exposed_delayed_trade_events", 0))
            ) > 0
            max_lag = max(
                float(values.get("max_exposed_stale_event_lag_ms", 0)),
                float(values.get("max_exposed_delayed_trade_lag_ms", 0)),
            )
        except (TypeError, ValueError):
            max_lag = 0.0
        quality_by_window[(str(strategy), str(slug))] = (
            "lagged" if lagged else "clean", max_lag,
        )
    totals: dict[tuple[str, str], list[float]] = {}
    for strategy, slug, pnl, cash, residual, resid_shares in db.execute(
        "SELECT strategy,slug,pnl,cash,residual,resid_shares FROM settlements"
    ):
        quality, max_lag = quality_by_window.get(
            (str(strategy), str(slug)), ("unclassified", 0.0),
        )
        row = totals.setdefault((str(strategy), quality), [0, 0, 0, 0, 0, 0])
        row[0] += 1
        row[1] += float(pnl)
        row[2] += float(cash) + float(resid_shares) / 2
        row[3] += float(cash) + min(float(residual), float(resid_shares) - float(residual))
        row[4] += abs(2 * float(residual) - float(resid_shares))
        row[5] = max(row[5], max_lag)
    return [
        FeedQualitySnapshot(strategy, quality, int(values[0]), *values[1:])
        for (strategy, quality), values in sorted(totals.items())
    ]
