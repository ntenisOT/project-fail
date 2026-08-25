"""Small, testable best-ask cache for Polymarket market WebSocket events."""

from __future__ import annotations

import dataclasses
from typing import Mapping


@dataclasses.dataclass(frozen=True)
class BestAsk:
    price: float | None
    received_at: float


@dataclasses.dataclass(frozen=True)
class BookUpdate:
    token: str
    bid: float | None
    ask: float | None
    has_bid: bool
    has_ask: bool


def parse_book_updates(event: Mapping[str, object]) -> list[BookUpdate]:
    """Normalize snapshot, price-change, and best-bid/ask event shapes."""
    event_type = event.get("event_type")
    rows: list[Mapping[str, object]] = []
    if event_type == "book":
        bids, asks = event.get("bids") or [], event.get("asks") or []
        if not isinstance(bids, list) or not isinstance(asks, list):
            return []
        bid_prices = [float(str(row["price"])) for row in bids if isinstance(row, dict)]
        ask_prices = [float(str(row["price"])) for row in asks if isinstance(row, dict)]
        return [BookUpdate(str(event.get("asset_id") or ""),
                           max(bid_prices) if bid_prices else None,
                           min(ask_prices) if ask_prices else None, True, True)]
    if event_type == "price_change":
        changes = event.get("price_changes") or []
        rows = changes if isinstance(changes, list) else []
    elif event_type == "best_bid_ask":
        rows = [event]

    updates = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("asset_id"):
            continue
        has_bid, has_ask = "best_bid" in row, "best_ask" in row
        bid = None if row.get("best_bid") in (None, "") else float(str(row["best_bid"]))
        ask = None if row.get("best_ask") in (None, "") else float(str(row["best_ask"]))
        updates.append(BookUpdate(str(row["asset_id"]), bid, ask, has_bid, has_ask))
    return updates


class BestAskCache:
    def __init__(self) -> None:
        self._books: dict[str, BestAsk] = {}

    def get(self, token: str) -> BestAsk | None:
        return self._books.get(token)

    def drop(self, token: str) -> None:
        self._books.pop(token, None)

    def clear(self) -> None:
        self._books.clear()

    def apply(self, event: Mapping[str, object], received_at: float) -> set[str]:
        """Apply authoritative snapshots or best-ask values from delta events."""
        changed: set[str] = set()
        for update in parse_book_updates(event):
            if not update.token or not update.has_ask:
                continue
            self._books[update.token] = BestAsk(update.ask, received_at)
            changed.add(update.token)
        return changed
