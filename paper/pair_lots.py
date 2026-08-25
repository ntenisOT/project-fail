"""FIFO opposite-token lot matching and completion-delay metrics."""

from __future__ import annotations

import dataclasses


def weighted_quantile(samples: list[tuple[float, float]], quantile: float) -> float:
    """Return a weighted quantile, or zero for an empty sample."""
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    if not samples:
        return 0.0
    ordered = sorted(samples)
    target = quantile * sum(weight for _, weight in ordered)
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative + 1e-9 >= target:
            return value
    return ordered[-1][0]


@dataclasses.dataclass
class _Lot:
    shares: float
    price: float
    opened_at: float


class PairLots:
    """Match opposite-token fills while retaining price and timing evidence."""

    def __init__(self) -> None:
        self.lots: dict[bool, list[_Lot]] = {True: [], False: []}
        self.paired_shares = 0.0
        self.paired_value = 0.0
        self.completion_delays: list[tuple[float, float]] = []

    @property
    def open_side(self) -> bool | None:
        sides = [side for side in (True, False) if self.lots[side]]
        if len(sides) > 1:
            raise RuntimeError("opposite unmatched pair lots")
        return sides[0] if sides else None

    def worst_open_price(self, buying: bool) -> float:
        side = self.open_side
        if side is None:
            raise RuntimeError("no open pair lot")
        prices = [lot.price for lot in self.lots[side]]
        return max(prices) if buying else min(prices)

    @property
    def open_shares(self) -> float:
        side = self.open_side
        return sum(lot.shares for lot in self.lots[side]) if side is not None else 0.0

    @property
    def open_value(self) -> float:
        side = self.open_side
        return (
            sum(lot.shares * lot.price for lot in self.lots[side])
            if side is not None else 0.0
        )

    def add(self, side: bool, shares: float, price: float, filled_at: float) -> None:
        remaining = shares
        opposite = self.lots[not side]
        while remaining > 1e-9 and opposite:
            lot = opposite[0]
            matched = min(remaining, lot.shares)
            self.paired_shares += matched
            self.paired_value += matched * (price + lot.price)
            self.completion_delays.append((max(0.0, filled_at - lot.opened_at), matched))
            remaining -= matched
            lot.shares -= matched
            if lot.shares <= 1e-9:
                opposite.pop(0)
        if remaining > 1e-9:
            self.lots[side].append(_Lot(remaining, price, filled_at))

    def delay_quantile(self, quantile: float) -> float:
        """Share-weighted completion-delay quantile; zero when nothing paired."""
        return weighted_quantile(self.completion_delays, quantile)

    def next_pair_sum_cap(self, cap: float, shares: float) -> float:
        """Maximum next pair sum that preserves the cumulative average cap."""
        return (cap * (self.paired_shares + shares) - self.paired_value) / shares

    def completion_price_cap(self, cap: float, quote_shares: float) -> float:
        """Maximum opposite price after crediting completed-pair surplus."""
        side = self.open_side
        if side is None:
            raise RuntimeError("no open pair lot")
        remaining = min(quote_shares, self.open_shares)
        matched = remaining
        open_value = 0.0
        for lot in self.lots[side]:
            take = min(remaining, lot.shares)
            open_value += take * lot.price
            remaining -= take
            if remaining <= 1e-9:
                break
        return (cap * (self.paired_shares + matched)
                - self.paired_value - open_value) / matched
