"""Pure planning and targeted cleanup for the bounded order-latency probe."""

from __future__ import annotations

import dataclasses
import math
import time
from typing import Protocol

from paper.market_metadata import ActiveMarket


@dataclasses.dataclass(frozen=True)
class ProbeOrder:
    outcome: str
    token: str
    price: float
    size: float
    best_bid: float
    tick_size: str
    neg_risk: bool

    @property
    def notional(self) -> float:
        return self.price * self.size


class OrderClient(Protocol):
    def cancel_orders(self, order_hashes: list[str]) -> object: ...
    def get_open_orders(self) -> object: ...


def _best_bid(book: dict[str, object]) -> float:
    bids = book.get("bids")
    if not isinstance(bids, list) or not bids:
        raise RuntimeError("active token has no bids")
    return max(float(str(row["price"])) for row in bids if isinstance(row, dict))


def choose_probe_order(
    market: ActiveMarket, books: dict[str, dict[str, object]], max_usd: float,
) -> ProbeOrder:
    """Choose five shares at least 20 cents behind the stronger token's best bid."""
    outcome, token = max(
        (("Up", market.up_token), ("Down", market.down_token)),
        key=lambda row: _best_bid(books[row[1]]),
    )
    book = books[token]
    best_bid = _best_bid(book)
    tick_text = str(book.get("tick_size") or "0.01")
    tick = float(tick_text)
    size = max(5.0, float(str(book.get("min_order_size") or 5.0)))
    distant = math.floor((best_bid - 0.20 + 1e-12) / tick) * tick
    cap_price = math.floor((max_usd / size + 1e-12) / tick) * tick
    price = round(min(distant, cap_price), 10)
    if price < 0.20 or price >= best_bid - tick / 2:
        raise RuntimeError("no safely distant probe price in the active book")
    if price * size < 1.0 - 1e-9 or price * size > max_usd + 1e-9:
        raise RuntimeError("probe cannot satisfy the $1 minimum and dollar cap")
    return ProbeOrder(
        outcome, token, price, size, best_bid, tick_text,
        bool(book.get("neg_risk", False)),
    )


def open_order_ids(rows: object) -> set[str]:
    if not isinstance(rows, list):
        raise RuntimeError("unexpected open-orders response")
    return {
        str(row.get("id") or row.get("orderID") or row.get("order_id"))
        for row in rows if isinstance(row, dict)
        if row.get("id") or row.get("orderID") or row.get("order_id")
    }


def cancel_and_verify(client: OrderClient, order_ids: set[str]) -> float:
    if not order_ids:
        return 0.0
    started = time.perf_counter()
    pending = set(order_ids)
    for attempt in range(3):
        client.cancel_orders(sorted(pending))
        time.sleep(0.10 * (attempt + 1))
        pending &= open_order_ids(client.get_open_orders())
        if not pending:
            return 1000 * (time.perf_counter() - started)
    raise RuntimeError(f"target order remains open after cancellation: {sorted(pending)}")
