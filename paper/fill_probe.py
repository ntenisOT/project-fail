"""Bounded maker-fill age and short-horizon markout telemetry."""

from __future__ import annotations

import dataclasses

from paper.order_book import OrderBook


HORIZONS_S = (1.0, 5.0, 15.0)


@dataclasses.dataclass
class _Fill:
    filled_at: float
    side_up: bool
    order_side: str
    price: float
    shares: float
    observed: set[float] = dataclasses.field(default_factory=set)


def _mid(book: OrderBook) -> float | None:
    if book.best_bid is None or book.best_ask is None:
        return None
    return (book.best_bid + book.best_ask) / 2


class FillProbe:
    """Measure maker order age and signed post-fill midpoint movement."""

    def __init__(self) -> None:
        self.ages: list[tuple[float, float]] = []
        self.markouts: dict[float, list[tuple[float, float, float]]] = {
            horizon: [] for horizon in HORIZONS_S
        }
        self._fills: list[_Fill] = []

    def record(self, filled_at: float, side_up: bool, order_side: str,
               price: float, shares: float, order_age_s: float) -> None:
        self.ages.append((max(0.0, order_age_s), shares))
        self._fills.append(_Fill(
            filled_at, side_up, order_side, price, shares,
        ))

    def observe(self, now: float, up: OrderBook, down: OrderBook) -> None:
        mids = {True: _mid(up), False: _mid(down)}
        for fill in self._fills:
            mid = mids[fill.side_up]
            if mid is None:
                continue
            for horizon in HORIZONS_S:
                due_at = fill.filled_at + horizon
                if horizon in fill.observed or now + 1e-9 < due_at:
                    continue
                edge = mid - fill.price if fill.order_side == "buy" else fill.price - mid
                self.markouts[horizon].append((edge, fill.shares, now - due_at))
                fill.observed.add(horizon)

    def metrics(self) -> dict[str, object]:
        return {
            "maker_fill_ages": self.ages,
            "maker_markouts": {
                str(int(horizon)): samples
                for horizon, samples in self.markouts.items()
            },
        }
