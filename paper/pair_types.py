"""Configuration and small state records for paired-inventory paper arms."""

from __future__ import annotations

import dataclasses
from typing import Literal


@dataclasses.dataclass(frozen=True)
class PairConfig:
    name: str
    mode: Literal[
        "accumulate", "churn", "mint", "inventory", "momentum",
        "terminal_momentum",
    ]
    requote_s: float
    action_latency_s: float = 0.065
    buy_sum_ceiling: float = 0.99
    sell_sum_floor: float = 1.01
    clip_shares: float = 5.0
    max_inventory: float = 20.0
    mint_sets: float = 20.0
    initial_sets: float = 0.0
    new_pair_cutoff_s: float = 300.0
    # Taker-flatten any naked residual this many seconds into the window.
    # Gen91 (11 windows) had every arm positive on FIFO-paired edge and deeply
    # negative on settlement outcome - basket99 +$3.86 edge against -$7.21
    # outcome - so the leftover leg gives back more than the pair mechanic
    # earns, and it does so on all four arms at once, which is adverse
    # selection rather than luck. Ledger analysis puts only 10% of residual
    # shares after T+240 and 60% before T+180, so refusing late pair opens
    # would barely touch it; the residual is created mid-window and never
    # completes. Selling it at a known price with a known fee converts an
    # uncontrolled coin flip into a measured cost. None keeps the old
    # behaviour so an arm can act as the control.
    flatten_residual_s: float | None = None
    # TWAP arm: take the favoured side this many seconds into the window.
    # None disables it. Both review seats returned NO-GO on trading the
    # backtest of this; the arm exists to answer them with queue-aware fills
    # on official outcomes rather than with a replayed tape.
    twap_entry_s: float | None = None
    twap_min_bps: float = 1.0
    twap_max_age_s: float = 10.0
    buy_taker_after_s: float | None = None
    taker_hedge_after_s: float | None = None
    taker_pair_sum_floor: float | None = None
    taker_dust_round_shares: float = 0.0
    improve_ticks: int = 0
    new_pair_start_s: float = 0.0
    mint_anchor_spread: float | None = None
    require_both_to_start: bool = False
    # Winner 0x1Dd2A69e runs a 6.5-share median Up/Down imbalance (p90 16.5,
    # max 43) and never stops quoting. The 0.1 default froze our mint arms
    # after the first asymmetric fill.
    imbalance_tolerance: float = 0.1
    # When the book is too tight to satisfy buy_sum_ceiling, a patient maker
    # rests BELOW the book at the price it is willing to pay and waits. The
    # default refuses to quote at all, which makes any ceiling under ~0.99
    # structurally inert rather than selective (observed Gen83).
    patient_bids: bool = False
    # momentum mode: measured on 600 BTC windows (tools/momentum_probe.py).
    # A >=10c move over 10s predicts enough continuation to clear the 3.5c
    # round-trip taker fee: net +0.0196/share over 1,030 trades. Below 5c the
    # fee eats the edge, so the threshold is deliberately high and it fires
    # about 1.7 times per window.
    momentum_threshold: float = 0.10
    momentum_lookback_s: float = 10.0
    momentum_hold_s: float = 30.0
    # Winner-like terminal momentum is deliberately separate from the retired
    # round-trip arm. It waits through modeled taker latency, refuses to chase
    # more than this many ticks, and holds any fill to official settlement.
    momentum_chase_ticks: int = 1
    momentum_cooldown_s: float = 20.0
    momentum_max_entries: int = 2
    basket_average_cap: bool = False
    ladder_offsets: tuple[int, ...] = ()
    quote_hold_s: float = 0.0


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
