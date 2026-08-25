"""Displayed-depth, fee-aware taker fills for paper experiments."""

from __future__ import annotations

import dataclasses
from typing import Literal

from paper.order_book import OrderBook

CRYPTO_TAKER_RATE = 0.07
MIN_NOTIONAL = 1.0


@dataclasses.dataclass(frozen=True)
class TakerLeg:
    price: float
    shares: float
    fee: float


def crypto_fee(price: float, shares: float) -> float:
    """Official crypto taker formula, rounded per match to five decimals."""
    return round(shares * CRYPTO_TAKER_RATE * price * (1 - price), 5)


def sweep(book: OrderBook, side: Literal["buy", "sell"], shares: float) -> list[TakerLeg]:
    """Return a full displayed-depth fill, or no fill when FOK cannot complete."""
    levels = book.asks if side == "buy" else book.bids
    prices = sorted(levels, reverse=side == "sell")
    remaining = shares
    legs: list[TakerLeg] = []
    for price in prices:
        size = min(remaining, levels[price])
        if size > 0:
            legs.append(TakerLeg(price, size, crypto_fee(price, size)))
            remaining -= size
        if remaining <= 1e-9:
            break
    gross = sum(leg.price * leg.shares for leg in legs)
    return legs if remaining <= 1e-9 and gross >= MIN_NOTIONAL else []
