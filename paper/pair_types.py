"""Configuration and small state records for paired-inventory paper arms."""

from __future__ import annotations

import dataclasses
from typing import Literal


@dataclasses.dataclass(frozen=True)
class PairConfig:
    name: str
    mode: Literal["accumulate", "churn", "mint", "inventory"]
    requote_s: float
    action_latency_s: float = 0.065
    buy_sum_ceiling: float = 0.99
    sell_sum_floor: float = 1.01
    clip_shares: float = 5.0
    max_inventory: float = 20.0
    mint_sets: float = 20.0
    initial_sets: float = 0.0
    new_pair_cutoff_s: float = 300.0
    buy_taker_after_s: float | None = None
    taker_hedge_after_s: float | None = None
    taker_pair_sum_floor: float | None = None
    improve_ticks: int = 0
    new_pair_start_s: float = 0.0
    mint_anchor_spread: float | None = None
    require_both_to_start: bool = False
    basket_average_cap: bool = False


@dataclasses.dataclass
class RestingOrder:
    price: float
    size: float
    queue_ahead: float
    placed_at: float


@dataclasses.dataclass
class PendingRequote:
    ready_at: float
    decided_at: float
