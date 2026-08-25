"""Fee-aware planning for taker completion of an unmatched buy leg."""

from __future__ import annotations

import dataclasses

from paper.order_book import OrderBook
from paper.pair_lots import PairLots
from paper.pair_types import PairConfig
from paper.taker import TakerLeg, sweep


@dataclasses.dataclass(frozen=True)
class BuyCompletionPlan:
    side: bool
    legs: tuple[TakerLeg, ...]


def plan_buy_completion(
    elapsed_s: float,
    opened_elapsed_s: float | None,
    config: PairConfig,
    pairs: PairLots,
    inventory: dict[bool, float],
    up: OrderBook,
    down: OrderBook,
) -> BuyCompletionPlan | None:
    after = config.buy_taker_after_s
    open_side = pairs.open_side
    if (after is None or open_side is None or opened_elapsed_s is None
            or elapsed_s < max(after, opened_elapsed_s) + config.action_latency_s):
        return None
    hedge_side = not open_side
    capacity = config.max_inventory - inventory[hedge_side]
    requested_shares = min(pairs.open_shares, capacity)
    book = up if hedge_side else down
    target_shares = requested_shares
    dust = config.taker_dust_round_shares
    if (dust > 0 and book.min_order_size - dust <= requested_shares
            < book.min_order_size and capacity + 1e-9 >= book.min_order_size):
        target_shares = book.min_order_size
    legs = sweep(book, "buy", target_shares)
    if not legs:
        return None
    shares = sum(leg.shares for leg in legs)
    total_cost = sum(leg.price * leg.shares + leg.fee for leg in legs)
    cap = (
        pairs.completion_price_cap(config.buy_sum_ceiling, shares)
        if config.basket_average_cap else
        config.buy_sum_ceiling - pairs.worst_open_price(True)
    )
    if total_cost / shares > cap + 1e-9:
        return None
    return BuyCompletionPlan(hedge_side, tuple(legs))
