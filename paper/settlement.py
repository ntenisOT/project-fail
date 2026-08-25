"""Per-strategy settlement selection for partially invalid paper cohorts."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

from paper.pair_engine import PairWindow


@dataclasses.dataclass(frozen=True)
class ScoredWindow:
    strategy: str
    settlement: dict[str, float | int]
    metrics: dict[str, object]


@dataclasses.dataclass(frozen=True)
class SkippedWindow:
    strategy: str
    slug: str
    reason: str
    n_fills: int
    capital: float
    cash: float
    up_shares: float
    down_shares: float
    event_lag_ms: float | None


def settle_valid(
    windows: Mapping[str, PairWindow], now: float, outcome_up: int,
) -> tuple[list[ScoredWindow], list[SkippedWindow]]:
    scored: list[ScoredWindow] = []
    skipped: list[SkippedWindow] = []
    for strategy, window in windows.items():
        if not window.full_window:
            skipped.append(SkippedWindow(
                strategy, window.slug, window.invalid_reason or "unknown",
                window.buys + window.sells, window.peak, window.cash,
                window.inventory[True], window.inventory[False],
                window.invalid_event_lag_ms,
            ))
            continue
        settlement, metrics = window.settle(now, outcome_up)
        scored.append(ScoredWindow(strategy, settlement, metrics))
    return scored, skipped
