"""Queue-aware, maker-only complete-set inventory simulation."""

from __future__ import annotations

import dataclasses
import math
from typing import Literal

from paper.order_book import OrderBook


@dataclasses.dataclass(frozen=True)
class PairConfig:
    name: str
    mode: Literal["accumulate", "churn", "mint"]
    requote_s: float
    action_latency_s: float = 0.065
    buy_sum_ceiling: float = 0.99
    sell_sum_floor: float = 1.01
    clip_shares: float = 5.0
    max_inventory: float = 20.0
    mint_sets: float = 20.0


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


class PairLots:
    """Match opposite-token fills and retain the exact open-leg prices."""

    def __init__(self) -> None:
        self.lots: dict[bool, list[list[float]]] = {True: [], False: []}
        self.paired_shares = 0.0
        self.paired_value = 0.0

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
        prices = [lot[1] for lot in self.lots[side]]
        return max(prices) if buying else min(prices)

    def add(self, side: bool, shares: float, price: float) -> None:
        remaining = shares
        opposite = self.lots[not side]
        while remaining > 1e-9 and opposite:
            lot = opposite[0]
            matched = min(remaining, lot[0])
            self.paired_shares += matched
            self.paired_value += matched * (price + lot[1])
            remaining -= matched
            lot[0] -= matched
            if lot[0] <= 1e-9:
                opposite.pop(0)
        if remaining > 1e-9:
            self.lots[side].append([remaining, price])


def _tick_price(value: float, tick: float, round_up: bool) -> float:
    units = math.ceil((value - 1e-9) / tick) if round_up else math.floor((value + 1e-9) / tick)
    return round(units * tick, 10)


class PairWindow:
    """Quotes both tokens while preserving pair balance and public queue depth."""

    def __init__(self, config: PairConfig, asset: str, slug: str, start: int,
                 up_token: str, down_token: str, observed_at: float | None = None) -> None:
        self.config, self.asset, self.slug = config, asset, slug
        self.start, self.end = start, start + 300
        self.full_window = (start if observed_at is None else observed_at) <= start + 10
        self.tokens = {True: up_token, False: down_token}
        minted = config.mint_sets if config.mode == "mint" and self.full_window else 0.0
        self.inventory = {True: minted, False: minted}
        self.cash = -minted
        self.peak = minted
        self.orders: dict[tuple[bool, str], RestingOrder] = {}
        self.pending: PendingRequote | None = None
        self.last_requote = -1e18
        self.buys = self.sells = 0
        self.quote_posts = self.quote_cancels = self.closed_orders = 0
        self.rest_seconds = self.queue_consumed = self.filled_shares = 0.0
        self.action_seconds = 0.0
        self.action_batches = self.post_only_rejects = 0
        self.buy_pairs = PairLots()
        self.sell_pairs = PairLots()

    def _desired(self, up: OrderBook, down: OrderBook) -> dict[tuple[bool, str], float]:
        books = {True: up, False: down}
        desired: dict[tuple[bool, str], float] = {}
        up_bid, down_bid = up.best_bid, down.best_bid
        if self.config.mode != "mint" and up_bid is not None and down_bid is not None:
            open_side = self.buy_pairs.open_side
            buy_sides = (True, False) if open_side is None else (not open_side,)
            for side in buy_sides:
                inv, other = self.inventory[side], self.inventory[not side]
                if inv > other + 0.1 or inv + self.config.clip_shares > self.config.max_inventory:
                    continue
                price = books[side].best_bid
                assert price is not None
                if open_side is None:
                    if up_bid + down_bid > self.config.buy_sum_ceiling:
                        continue
                else:
                    cap = (self.config.buy_sum_ceiling
                           - self.buy_pairs.worst_open_price(buying=True))
                    price = min(price, _tick_price(cap, books[side].tick, round_up=False))
                if 0 < price < 1:
                    desired[(side, "buy")] = price

        up_ask, down_ask = up.best_ask, down.best_ask
        if (self.config.mode in ("churn", "mint")
                and up_ask is not None and down_ask is not None):
            open_side = self.sell_pairs.open_side
            sell_sides = (True, False) if open_side is None else (not open_side,)
            for side in sell_sides:
                inv, other = self.inventory[side], self.inventory[not side]
                if inv + 0.1 >= other and inv >= self.config.clip_shares:
                    price = books[side].best_ask
                    assert price is not None
                    if open_side is None:
                        if up_ask + down_ask < self.config.sell_sum_floor:
                            continue
                    else:
                        floor = (self.config.sell_sum_floor
                                 - self.sell_pairs.worst_open_price(buying=False))
                        price = max(price, _tick_price(floor, books[side].tick, round_up=True))
                    if 0 < price < 1:
                        desired[(side, "sell")] = price
        return desired

    def _close_order(self, key: tuple[bool, str], now: float, cancelled: bool) -> None:
        order = self.orders.pop(key, None)
        if order is None:
            return
        self.rest_seconds += max(0.0, now - order.placed_at)
        self.closed_orders += 1
        self.quote_cancels += int(cancelled)

    def _activate_pending(self, now: float, up: OrderBook, down: OrderBook) -> None:
        pending = self.pending
        if pending is None or now < pending.ready_at:
            return
        self.pending = None
        self.action_seconds += max(0.0, now - pending.decided_at)
        self.action_batches += 1
        complete = (up.best_bid is not None and down.best_bid is not None
                    and up.best_ask is not None and down.best_ask is not None)
        desired = self._desired(up, down) if complete else {}
        books = {True: up, False: down}
        for key in list(self.orders):
            if key not in desired or self.orders[key].price != desired[key]:
                self._close_order(key, now, cancelled=True)
        for key, price in desired.items():
            if key in self.orders:
                continue
            side, order_side = key
            book = books[side]
            crossed = (order_side == "buy" and book.best_ask is not None
                       and price >= book.best_ask)
            crossed = crossed or (order_side == "sell" and book.best_bid is not None
                                   and price <= book.best_bid)
            if crossed:
                self.post_only_rejects += 1
                continue
            self.orders[key] = RestingOrder(
                price, self.config.clip_shares,
                book.size_at(order_side, price), now,
            )
            self.quote_posts += 1

    def on_books(self, now: float, up: OrderBook, down: OrderBook) -> None:
        if not self.full_window or now < self.start or now >= self.end:
            return
        self._activate_pending(now, up, down)
        if self.pending is not None or now - self.last_requote < self.config.requote_s:
            return
        complete = (up.best_bid is not None and down.best_bid is not None
                    and up.best_ask is not None and down.best_ask is not None)
        desired = self._desired(up, down) if complete else {}
        self.last_requote = now
        current = {key: order.price for key, order in self.orders.items()}
        if current == desired:
            return
        self.pending = PendingRequote(
            now + self.config.action_latency_s, now,
        )
        self._activate_pending(now, up, down)

    def on_trade(self, now: float, side_up: bool, price: float, size: float,
                 taker_side: str) -> dict[str, float | str] | None:
        if taker_side.upper() not in ("BUY", "SELL"):
            return None
        order_side = "buy" if taker_side.upper() == "SELL" else "sell"
        key = (side_up, order_side)
        order = self.orders.get(key)
        if order is None or now >= self.end:
            return None
        crossed = price <= order.price if order_side == "buy" else price >= order.price
        if not crossed:
            return None
        if price == order.price:
            consumed = min(order.queue_ahead, size)
            order.queue_ahead -= consumed
            self.queue_consumed += consumed
            executable = max(0.0, size - consumed)
        else:
            executable = order.size
        capacity = (self.config.max_inventory - self.inventory[side_up]
                    if order_side == "buy" else self.inventory[side_up])
        if capacity <= 0:
            self._close_order(key, now, cancelled=True)
            return None
        fill = min(order.size, executable, max(0.0, capacity))
        if fill <= 0:
            return None
        order.size -= fill
        notional = fill * order.price
        self.filled_shares += fill
        if order_side == "buy":
            self.inventory[side_up] += fill
            self.cash -= notional
            self.peak = max(self.peak, -self.cash)
            self.buys += 1
            self.buy_pairs.add(side_up, fill, order.price)
            signed_cash = -notional
        else:
            self.inventory[side_up] -= fill
            self.cash += notional
            self.sells += 1
            self.sell_pairs.add(side_up, fill, order.price)
            signed_cash = notional
        if order.size <= 1e-9:
            self._close_order(key, now, cancelled=False)
        return {"action": order_side, "price": order.price, "size": fill,
                "signed_cash": signed_cash, "outcome_up": int(side_up)}

    def settle(self, now: float, outcome_up: int) -> tuple[dict[str, float | int], dict[str, float]]:
        self.pending = None
        for key in list(self.orders):
            self._close_order(key, now, cancelled=True)
        if (min(self.inventory.values()) < -1e-8
                or max(self.inventory.values()) > self.config.max_inventory + 1e-8):
            raise RuntimeError(f"inventory invariant violated in {self.slug}: {self.inventory}")
        residual = self.inventory[True] * outcome_up + self.inventory[False] * (1 - outcome_up)
        paired = min(self.inventory.values())
        fills = self.buys + self.sells
        settlement = {
            "cash": self.cash, "residual": residual, "pnl": self.cash + residual,
            "capital": self.peak, "buys": self.buys, "sells": self.sells,
            "resid_shares": sum(self.inventory.values()),
            "n_fills": fills,
            "outcome_up": outcome_up,
        }
        metrics = {
            "quote_posts": self.quote_posts, "quote_cancels": self.quote_cancels,
            "closed_orders": self.closed_orders, "rest_seconds": self.rest_seconds,
            "queue_consumed": self.queue_consumed, "filled_shares": self.filled_shares,
            "action_seconds": self.action_seconds, "action_batches": self.action_batches,
            "post_only_rejects": self.post_only_rejects,
            "paired_end": paired, "unmatched_end": abs(self.inventory[True] - self.inventory[False]),
            "buy_pair_shares": self.buy_pairs.paired_shares,
            "buy_pair_cost": self.buy_pairs.paired_value,
            "sell_pair_shares": self.sell_pairs.paired_shares,
            "sell_pair_proceeds": self.sell_pairs.paired_value,
        }
        return settlement, metrics
