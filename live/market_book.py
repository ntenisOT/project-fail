"""Small, testable best-ask cache for Polymarket market WebSocket events."""

from __future__ import annotations

import dataclasses
import math
from typing import Mapping

from live.feed_health import MAX_FUTURE_EVENT_SKEW_S


@dataclasses.dataclass(frozen=True)
class BestAsk:
    price: float | None
    received_at: float
    source_at: float


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
        bids, asks = event.get("bids"), event.get("asks")
        if not isinstance(bids, list) or not isinstance(asks, list):
            raise ValueError("book bids and asks must be lists")
        token = str(event.get("asset_id") or "")
        if not token:
            raise ValueError("book lacks asset_id")
        bid_prices = [_level_price(row) for row in bids]
        ask_prices = [_level_price(row) for row in asks]
        return [BookUpdate(token,
                           max(bid_prices) if bid_prices else None,
                           min(ask_prices) if ask_prices else None, True, True)]
    if event_type == "price_change":
        changes = event.get("price_changes")
        if not isinstance(changes, list) or not changes:
            raise ValueError("price_change lacks rows")
        rows = changes
    elif event_type == "best_bid_ask":
        rows = [event]

    updates = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("asset_id"):
            raise ValueError("BBO update lacks asset_id")
        has_bid, has_ask = "best_bid" in row, "best_ask" in row
        bid = _optional_price(row.get("best_bid")) if has_bid else None
        ask = _optional_price(row.get("best_ask")) if has_ask else None
        updates.append(BookUpdate(str(row["asset_id"]), bid, ask, has_bid, has_ask))
    return updates


def _level_price(row: object) -> float:
    if not isinstance(row, dict) or "price" not in row:
        raise ValueError("book level lacks price")
    price = _optional_price(row["price"])
    if price is None:
        raise ValueError("book level price cannot be empty")
    return price


def _optional_price(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        price = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid BBO price") from exc
    if not math.isfinite(price) or not 0 < price < 1:
        raise ValueError("BBO price is outside (0, 1)")
    return price


class BestAskCache:
    def __init__(self) -> None:
        self._books: dict[str, BestAsk] = {}
        self._revisions: dict[str, int] = {}

    def get(self, token: str) -> BestAsk | None:
        return self._books.get(token)

    def drop(self, token: str) -> None:
        self._books.pop(token, None)
        self._revisions[token] = self._revisions.get(token, 0) + 1

    def clear(self) -> None:
        for token in self._books.keys() | self._revisions.keys():
            self._revisions[token] = self._revisions.get(token, 0) + 1
        self._books.clear()

    def revision(self, *tokens: str) -> tuple[int, ...]:
        """Return a plan identity that unrelated asset updates cannot change."""
        return tuple(self._revisions.get(token, 0) for token in tokens)

    def apply(
        self, event: Mapping[str, object], received_at: float, *,
        source_at: float | None = None,
    ) -> set[str]:
        """Apply authoritative BBO values without allowing time to move backward."""
        source_time = received_at if source_at is None else source_at
        if (not math.isfinite(received_at) or not math.isfinite(source_time)
                or received_at <= 0 or source_time <= 0):
            raise ValueError("BBO timestamps must be finite and positive")
        changed: set[str] = set()
        for update in parse_book_updates(event):
            if not update.token or not update.has_ask:
                continue
            previous = self._books.get(update.token)
            if previous is not None and source_time < previous.source_at:
                continue
            self._books[update.token] = BestAsk(
                update.ask, received_at, source_time,
            )
            changed.add(update.token)
        if changed:
            for token in changed:
                self._revisions[token] = self._revisions.get(token, 0) + 1
        return changed


def fresh_ask_pair(
    books: BestAskCache, up_token: str, down_token: str,
    now: float, max_age_s: float,
) -> tuple[float, float] | None:
    """Return both asks only when each token has a recent causal update."""
    up, down = books.get(up_token), books.get(down_token)
    if (up is None or down is None or up.price is None or down.price is None
            or now < up.received_at or now < down.received_at
            or now - up.received_at > max_age_s
            or now - down.received_at > max_age_s
            or now - up.source_at > max_age_s
            or now - down.source_at > max_age_s
            or up.source_at - now > MAX_FUTURE_EVENT_SKEW_S
            or down.source_at - now > MAX_FUTURE_EVENT_SKEW_S):
        return None
    return up.price, down.price
