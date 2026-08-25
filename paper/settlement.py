"""Per-strategy settlement selection for partially invalid paper cohorts."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

from paper.pair_engine import PairWindow


@dataclasses.dataclass(frozen=True)
class ScoredWindow:
    strategy: str
    settlement: dict[str, float | int]
    metrics: dict[str, float]


@dataclasses.dataclass(frozen=True)
class SkippedWindow:
    strategy: str
    slug: str
    reason: str


def settle_valid(
    windows: Mapping[str, PairWindow], now: float, outcome_up: int,
) -> tuple[list[ScoredWindow], list[SkippedWindow]]:
    scored: list[ScoredWindow] = []
    skipped: list[SkippedWindow] = []
    for strategy, window in windows.items():
        if not window.full_window:
            skipped.append(SkippedWindow(
                strategy, window.slug, window.invalid_reason or "unknown",
            ))
            continue
        settlement, metrics = window.settle(now, outcome_up)
        scored.append(ScoredWindow(strategy, settlement, metrics))
    return scored, skipped
