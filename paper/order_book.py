"""Small in-memory CLOB level cache for queue-aware paper fills."""

from __future__ import annotations

import dataclasses
from typing import Mapping


@dataclasses.dataclass
class OrderBook:
    bids: dict[float, float] = dataclasses.field(default_factory=dict)
    asks: dict[float, float] = dataclasses.field(default_factory=dict)
    received_at: float = 0.0
    tick: float = 0.01

    @property
    def best_bid(self) -> float | None:
        return max(self.bids, default=None)

    @property
    def best_ask(self) -> float | None:
        return min(self.asks, default=None)

    def size_at(self, side: str, price: float) -> float:
        levels = self.bids if side == "buy" else self.asks
        return levels.get(price, 0.0)


def _levels(raw: object) -> dict[float, float]:
    if not isinstance(raw, list):
        return {}
    result: dict[float, float] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            price, size = float(row["price"]), float(row["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < price < 1 and size > 0:
            result[price] = size
    return result


class OrderBookCache:
    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}

    def get(self, token: str) -> OrderBook | None:
        return self._books.get(token)

    def drop(self, token: str) -> None:
        self._books.pop(token, None)

    def clear(self) -> None:
        self._books.clear()

    def apply(self, event: Mapping[str, object], received_at: float) -> set[str]:
        """Apply official snapshot, price-level delta, or tick-size events."""
        event_type = event.get("event_type")
        changed: set[str] = set()
        if event_type == "book":
            token = str(event.get("asset_id") or "")
            if not token:
                return changed
            book = self._books.setdefault(token, OrderBook())
            book.bids = _levels(event.get("bids"))
            book.asks = _levels(event.get("asks"))
            book.received_at = received_at
            return {token}

        if event_type == "price_change":
            rows = event.get("price_changes") or []
            if not isinstance(rows, list):
                return changed
            for row in rows:
                if not isinstance(row, dict):
                    continue
                token = str(row.get("asset_id") or "")
                side = str(row.get("side") or "").upper()
                if not token or side not in ("BUY", "SELL"):
                    continue
                try:
                    price, size = float(row["price"]), float(row["size"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not 0 < price < 1 or size < 0:
                    continue
                book = self._books.setdefault(token, OrderBook())
                levels = book.bids if side == "BUY" else book.asks
                if size > 0:
                    levels[price] = size
                else:
                    levels.pop(price, None)
                book.received_at = received_at
                changed.add(token)
            return changed

        if event_type == "tick_size_change":
            token = str(event.get("asset_id") or "")
            try:
                tick = float(str(event.get("new_tick_size") or 0))
            except (TypeError, ValueError):
                return changed
            if token and tick > 0:
                book = self._books.setdefault(token, OrderBook())
                book.tick = tick
                book.received_at = received_at
                changed.add(token)
        return changed
