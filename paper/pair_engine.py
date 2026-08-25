"""Queue-aware complete-set inventory simulation."""

from __future__ import annotations

import math

from live.mint_quotes import guarded_pair_prices, plan_pair_quotes, should_reprice
from paper.buy_completion import plan_buy_completion
from paper.order_book import OrderBook
from paper.pair_lots import PairLots
from paper.pair_types import PairConfig, PendingRequote, RestingOrder
from paper.taker import crypto_maker_rebate, sweep


def _tick_price(value: float, tick: float, round_up: bool) -> float:
    units = math.ceil((value - 1e-9) / tick) if round_up else math.floor((value + 1e-9) / tick)
    return round(units * tick, 10)


def _maker_price(book: OrderBook, order_side: str, improve_ticks: int) -> float:
    if order_side == "buy":
        assert book.best_bid is not None and book.best_ask is not None
        return round(min(book.best_bid + improve_ticks * book.tick,
                         book.best_ask - book.tick), 10)
    assert book.best_bid is not None and book.best_ask is not None
    return round(max(book.best_ask - improve_ticks * book.tick,
                     book.best_bid + book.tick), 10)


class PairWindow:
    """Quotes both tokens while preserving pair balance and public queue depth."""

    def __init__(self, config: PairConfig, asset: str, slug: str, start: int,
                 up_token: str, down_token: str, observed_at: float | None = None) -> None:
        self.config, self.asset, self.slug = config, asset, slug
        self.start, self.end = start, start + 300
        self.full_window = (start if observed_at is None else observed_at) <= start + 10
        self.invalid_reason = None if self.full_window else "partial_startup"
        self.invalid_event_lag_ms: float | None = None
        self.first_books_at: float | None = None
        self.tokens = {True: up_token, False: down_token}
        starting_sets = config.mint_sets if config.mode == "mint" else config.initial_sets
        minted = starting_sets if self.full_window else 0.0
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
        self.action_batches = self.post_only_rejects = self.pre_activation_trades = 0
        self.taker_fees = 0.0
        self.maker_rebates = 0.0
        self.stale_market_events = self.exposed_stale_market_events = 0
        self.max_stale_event_lag_ms = self.max_exposed_stale_event_lag_ms = 0.0
        self.delayed_trade_events = self.exposed_delayed_trade_events = 0
        self.max_delayed_trade_lag_ms = self.max_exposed_delayed_trade_lag_ms = 0.0
        self.buy_opened_at: float | None = None
        self.sell_opened_at: float | None = None
        self.buy_pairs = PairLots()
        self.sell_pairs = PairLots()

    def _desired(self, now: float, up: OrderBook,
                 down: OrderBook) -> dict[tuple[bool, str], float]:
        if now < self.start + self.config.new_pair_start_s:
            return {}
        if self.config.mode == "mint" and self.config.mint_anchor_spread is not None:
            return self._mint_desired(now, up, down)
        books = {True: up, False: down}
        desired: dict[tuple[bool, str], float] = {}
        can_start_pair = now < self.start + self.config.new_pair_cutoff_s
        replenishing = (self.config.mode == "inventory"
                        and min(self.inventory.values()) < self.config.max_inventory - 0.1)
        inventory_can_buy = (
            self.config.mode != "inventory"
            or self.buy_pairs.open_side is not None
            or (replenishing and self.sell_pairs.open_side is None)
        )
        inventory_can_sell = (
            self.config.mode != "inventory"
            or self.sell_pairs.open_side is not None
            or (not replenishing and self.buy_pairs.open_side is None)
        )
        up_bid, down_bid = up.best_bid, down.best_bid
        if (self.config.mode != "mint"
                and inventory_can_buy
                and up_bid is not None and down_bid is not None):
            open_side = self.buy_pairs.open_side
            improved_bids = {
                side: _maker_price(books[side], "buy", self.config.improve_ticks)
                for side in (True, False)
            }
            buy_sides = (
                ((True, False) if can_start_pair else ())
                if open_side is None else (not open_side,)
            )
            start_cap = self.config.buy_sum_ceiling
            if self.config.basket_average_cap:
                start_cap = self.buy_pairs.next_pair_sum_cap(
                    self.config.buy_sum_ceiling, self.config.clip_shares,
                )
            if open_side is None and sum(improved_bids.values()) > start_cap:
                buy_sides = ()
            candidates: dict[tuple[bool, str], float] = {}
            for side in buy_sides:
                inv, other = self.inventory[side], self.inventory[not side]
                if inv > other + 0.1 or inv + self.config.clip_shares > self.config.max_inventory:
                    continue
                price = improved_bids[side]
                if open_side is not None:
                    cap = (self.buy_pairs.completion_price_cap(
                               self.config.buy_sum_ceiling, self.config.clip_shares,
                           ) if self.config.basket_average_cap else
                           self.config.buy_sum_ceiling
                           - self.buy_pairs.worst_open_price(buying=True))
                    price = min(price, _tick_price(cap, books[side].tick, round_up=False))
                if 0 < price < 1:
                    candidates[(side, "buy")] = price
            if not (self.config.require_both_to_start and open_side is None
                    and len(candidates) != 2):
                desired.update(candidates)

        up_ask, down_ask = up.best_ask, down.best_ask
        if (self.config.mode in ("churn", "mint", "inventory")
                and inventory_can_sell
                and up_ask is not None and down_ask is not None):
            open_side = self.sell_pairs.open_side
            improved_asks = {
                side: _maker_price(books[side], "sell", self.config.improve_ticks)
                for side in (True, False)
            }
            sell_sides = (
                ((True, False) if can_start_pair else ())
                if open_side is None else (not open_side,)
            )
            if open_side is None and sum(improved_asks.values()) < self.config.sell_sum_floor:
                sell_sides = ()
            candidates = {}
            for side in sell_sides:
                inv, other = self.inventory[side], self.inventory[not side]
                if inv + 0.1 >= other and inv >= self.config.clip_shares:
                    price = improved_asks[side]
                    if open_side is not None:
                        floor = (self.config.sell_sum_floor
                                 - self.sell_pairs.worst_open_price(buying=False))
                        price = max(price, _tick_price(floor, books[side].tick, round_up=True))
                    if 0 < price < 1:
                        candidates[(side, "sell")] = price
            if not (self.config.require_both_to_start and open_side is None
                    and len(candidates) != 2):
                desired.update(candidates)
        return desired

    def _mint_desired(self, now: float, up: OrderBook,
                      down: OrderBook) -> dict[tuple[bool, str], float]:
        assert up.best_ask is not None and down.best_ask is not None
        prices = guarded_pair_prices(
            up.best_ask, down.best_ask,
            spread=self.config.mint_anchor_spread or 0,
            sum_floor=self.config.sell_sum_floor,
        )
        if prices is None:
            return {}
        open_side = self.sell_pairs.open_side
        if open_side is not None:
            side = not open_side
            book = up if side else down
            floor = self.config.sell_sum_floor - self.sell_pairs.worst_open_price(False)
            completion_price = max(
                prices[0 if side else 1], _tick_price(floor, book.tick, True),
            )
            completion_order = self.orders.get((side, "sell"))
            if completion_order is not None and completion_order.price + 1e-9 >= floor:
                reprice = should_reprice(
                    (completion_order.price, completion_order.price),
                    (completion_price, completion_price),
                    now - completion_order.placed_at,
                )
                if not reprice:
                    completion_price = completion_order.price
            return ({(side, "sell"): completion_price}
                    if 0 < completion_price < 1 else {})
        if now >= self.start + self.config.new_pair_cutoff_s:
            return {}
        plan = plan_pair_quotes(
            minted=self.config.mint_sets,
            sold_up=self.config.mint_sets - self.inventory[True],
            sold_down=self.config.mint_sets - self.inventory[False],
            price_up=prices[0], price_down=prices[1],
            sum_floor=self.config.sell_sum_floor,
            clip_shares=self.config.clip_shares,
        )
        target = {(quote.side_up, "sell"): quote.price for quote in plan}
        current = {
            side: self.orders[(side, "sell")].price
            for side in (True, False) if (side, "sell") in self.orders
        }
        if len(current) == 2 and len(target) == 2:
            age = now - min(self.orders[(side, "sell")].placed_at for side in current)
            old = (current[True], current[False])
            new = (target[(True, "sell")], target[(False, "sell")])
            if not should_reprice(old, new, age):
                return {(side, "sell"): price for side, price in current.items()}
        return target

    def _close_order(self, key: tuple[bool, str], now: float, cancelled: bool) -> None:
        order = self.orders.pop(key, None)
        if order is None:
            return
        self.rest_seconds += max(0.0, now - order.placed_at)
        self.closed_orders += 1
        self.quote_cancels += int(cancelled)

    def _update_peak(self) -> None:
        reserved_bids = sum(
            order.price * order.size
            for (_, order_side), order in self.orders.items()
            if order_side == "buy"
        )
        self.peak = max(self.peak, max(0.0, -self.cash) + reserved_bids)

    def _activate_pending(self, now: float, up: OrderBook, down: OrderBook) -> None:
        pending = self.pending
        if pending is None or now < pending.ready_at:
            return
        self.pending = None
        self.action_seconds += max(0.0, now - pending.decided_at)
        self.action_batches += 1
        complete = (up.best_bid is not None and down.best_bid is not None
                    and up.best_ask is not None and down.best_ask is not None)
        desired = self._desired(now, up, down) if complete else {}
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
        self._update_peak()

    def _record_sell_pair(self, now: float, side: bool,
                          shares: float, net_price: float) -> None:
        before = self.sell_pairs.open_side
        self.sell_pairs.add(side, shares, net_price, now)
        after = self.sell_pairs.open_side
        if after is None:
            self.sell_opened_at = None
        elif before is None or after != before:
            self.sell_opened_at = now

    def _record_buy_pair(self, event_at: float, known_at: float, side: bool,
                         shares: float, net_price: float) -> None:
        before = self.buy_pairs.open_side
        self.buy_pairs.add(side, shares, net_price, event_at)
        after = self.buy_pairs.open_side
        if after is None:
            self.buy_opened_at = None
        elif before is None or after != before:
            self.buy_opened_at = known_at

    def _hedge_buy_pair(self, now: float, up: OrderBook,
                        down: OrderBook) -> list[dict[str, float | str]]:
        plan = plan_buy_completion(
            now - self.start, None if self.buy_opened_at is None else
            self.buy_opened_at - self.start,
            self.config, self.buy_pairs, self.inventory, up, down,
        )
        if plan is None:
            return []
        self._close_order((plan.side, "buy"), now, cancelled=True)
        records: list[dict[str, float | str]] = []
        for leg in plan.legs:
            cost = leg.price * leg.shares + leg.fee
            self.inventory[plan.side] += leg.shares
            self.cash -= cost
            self.filled_shares += leg.shares
            self.taker_fees += leg.fee
            self.buys += 1
            self._record_buy_pair(
                now, now, plan.side, leg.shares, cost / leg.shares,
            )
            records.append({
                "action": "taker_buy", "price": leg.price, "size": leg.shares,
                "signed_cash": -cost, "outcome_up": int(plan.side),
            })
        self._update_peak()
        return records

    def _hedge_sell_pair(self, now: float, up: OrderBook,
                         down: OrderBook) -> list[dict[str, float | str]]:
        delay = self.config.taker_hedge_after_s
        open_side = self.sell_pairs.open_side
        if (delay is None or open_side is None or self.sell_opened_at is None
                or now < self.sell_opened_at + delay + self.config.action_latency_s):
            return []
        hedge_side = not open_side
        shares = min(self.sell_pairs.open_shares, self.inventory[hedge_side])
        legs = sweep(up if hedge_side else down, "sell", shares)
        if not legs:
            return []
        net_cash = sum(leg.price * leg.shares - leg.fee for leg in legs)
        pair_floor = (self.config.sell_sum_floor
                      if self.config.taker_pair_sum_floor is None
                      else self.config.taker_pair_sum_floor)
        if (self.sell_pairs.worst_open_price(buying=False) + net_cash / shares
                < pair_floor - 1e-9):
            return []
        self._close_order((hedge_side, "sell"), now, cancelled=True)
        records: list[dict[str, float | str]] = []
        for leg in legs:
            net_cash = leg.price * leg.shares - leg.fee
            self.inventory[hedge_side] -= leg.shares
            self.cash += net_cash
            self.filled_shares += leg.shares
            self.taker_fees += leg.fee
            self.sells += 1
            self._record_sell_pair(
                now, hedge_side, leg.shares, net_cash / leg.shares,
            )
            records.append({
                "action": "taker_sell", "price": leg.price, "size": leg.shares,
                "signed_cash": net_cash, "outcome_up": int(hedge_side),
            })
        return records

    def on_books(self, now: float, up: OrderBook,
                 down: OrderBook) -> list[dict[str, float | str]]:
        if self.first_books_at is None:
            self.first_books_at = now
            if now > self.start + 10:
                self.full_window = False
                self.invalid_reason = "late_first_books"
        if not self.full_window or now < self.start or now >= self.end:
            return []
        self._activate_pending(now, up, down)
        records = self._hedge_buy_pair(now, up, down)
        records.extend(self._hedge_sell_pair(now, up, down))
        if self.pending is not None or now - self.last_requote < self.config.requote_s:
            return records
        complete = (up.best_bid is not None and down.best_bid is not None
                    and up.best_ask is not None and down.best_ask is not None)
        desired = self._desired(now, up, down) if complete else {}
        self.last_requote = now
        current = {key: order.price for key, order in self.orders.items()}
        if current == desired:
            return records
        self.pending = PendingRequote(
            now + self.config.action_latency_s, now,
        )
        self._activate_pending(now, up, down)
        return records

    def on_trade(self, now: float, side_up: bool, price: float, size: float,
                 taker_side: str, received_at: float | None = None,
                 ) -> dict[str, float | str] | None:
        known_at = now if received_at is None else received_at
        if taker_side.upper() not in ("BUY", "SELL"):
            return None
        order_side = "buy" if taker_side.upper() == "SELL" else "sell"
        key = (side_up, order_side)
        order = self.orders.get(key)
        if order is None or now >= self.end:
            return None
        if now + 1e-9 < order.placed_at:
            self.pre_activation_trades += 1
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
            # A through-print proves price priority was crossed, but it does not
            # prove more executable flow than the public print itself.
            executable = min(order.size, size)
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
        self.maker_rebates += crypto_maker_rebate(order.price, fill)
        if order_side == "buy":
            self.inventory[side_up] += fill
            self.cash -= notional
            self.peak = max(self.peak, -self.cash)
            self.buys += 1
            self._record_buy_pair(now, known_at, side_up, fill, order.price)
            signed_cash = -notional
        else:
            self.inventory[side_up] -= fill
            self.cash += notional
            self.sells += 1
            self._record_sell_pair(known_at, side_up, fill, order.price)
            signed_cash = notional
        if order.size <= 1e-9:
            self._close_order(key, now, cancelled=False)
        self._update_peak()
        return {"action": order_side, "price": order.price, "size": fill,
                "signed_cash": signed_cash, "outcome_up": int(side_up)}

    def invalidate(self, now: float, reason: str = "feed_gap",
                   event_lag_ms: float | None = None) -> None:
        """Stop scoring after a feed gap that can hide queue-consuming trades."""
        if self.full_window:
            self.invalid_reason = reason
            self.invalid_event_lag_ms = event_lag_ms
        self.full_window = False
        self.pending = None
        for key in list(self.orders):
            self._close_order(key, now, cancelled=True)

    def observe_stale_market_event(self, event_lag_ms: float | None) -> None:
        """Record a feed tail without pretending an exchange order disappeared."""
        lag_ms = 0.0 if event_lag_ms is None else event_lag_ms
        self.stale_market_events += 1
        self.max_stale_event_lag_ms = max(self.max_stale_event_lag_ms, lag_ms)
        if self.orders or self.pending is not None or self.buys or self.sells:
            self.exposed_stale_market_events += 1
            self.max_exposed_stale_event_lag_ms = max(
                self.max_exposed_stale_event_lag_ms, lag_ms,
            )

    def observe_delayed_trade_event(self, event_lag_ms: float) -> None:
        """Record late fill awareness separately from causal book staleness."""
        self.delayed_trade_events += 1
        self.max_delayed_trade_lag_ms = max(self.max_delayed_trade_lag_ms, event_lag_ms)
        if self.orders or self.pending is not None or self.buys or self.sells:
            self.exposed_delayed_trade_events += 1
            self.max_exposed_delayed_trade_lag_ms = max(
                self.max_exposed_delayed_trade_lag_ms, event_lag_ms,
            )

    def settle(self, now: float, outcome_up: int) -> tuple[dict[str, float | int], dict[str, object]]:
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
            "pre_activation_trades": self.pre_activation_trades,
            "stale_market_events": self.stale_market_events,
            "exposed_stale_market_events": self.exposed_stale_market_events,
            "max_stale_event_lag_ms": self.max_stale_event_lag_ms,
            "max_exposed_stale_event_lag_ms": self.max_exposed_stale_event_lag_ms,
            "delayed_trade_events": self.delayed_trade_events,
            "exposed_delayed_trade_events": self.exposed_delayed_trade_events,
            "max_delayed_trade_lag_ms": self.max_delayed_trade_lag_ms,
            "max_exposed_delayed_trade_lag_ms": self.max_exposed_delayed_trade_lag_ms,
            "taker_fees": self.taker_fees,
            "maker_rebates": self.maker_rebates,
            "paired_end": paired, "unmatched_end": abs(self.inventory[True] - self.inventory[False]),
            "buy_pair_shares": self.buy_pairs.paired_shares,
            "buy_pair_cost": self.buy_pairs.paired_value,
            "buy_pair_delays": self.buy_pairs.completion_delays,
            "sell_pair_shares": self.sell_pairs.paired_shares,
            "sell_pair_proceeds": self.sell_pairs.paired_value,
            "sell_pair_delays": self.sell_pairs.completion_delays,
        }
        return settlement, metrics
