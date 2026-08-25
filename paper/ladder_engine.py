"""Small, stable multi-level wrapper around the proven paired-bid lane."""

from __future__ import annotations

import dataclasses

from paper.order_book import OrderBook
from paper.pair_engine import PairWindow
from paper.pair_lots import PairLots
from paper.pair_types import PairConfig, RestingOrder


def _number(metrics: dict[str, object], key: str) -> float:
    value = metrics[key]
    if not isinstance(value, (int, float)):
        raise TypeError(f"metric {key} is not numeric")
    return float(value)


class _StableLane(PairWindow):
    """Retain a balanced quote pair long enough to earn queue position."""

    def _desired(
        self, now: float, up: OrderBook, down: OrderBook,
    ) -> dict[tuple[bool, str], float]:
        current = {
            side: self.orders[(side, "buy")]
            for side in (True, False) if (side, "buy") in self.orders
        }
        before_cutoff = now < self.start + self.config.new_pair_cutoff_s
        if (before_cutoff and self.buy_pairs.open_side is None
                and len(current) == 2 and self.config.quote_hold_s > 0):
            oldest = min(order.placed_at for order in current.values())
            if now - oldest < self.config.quote_hold_s:
                return {(side, "buy"): order.price for side, order in current.items()}
        return super()._desired(now, up, down)


class LadderWindow:
    """Run bounded price levels with shared reporting and FIFO pair evidence."""

    _SUM_METRICS = {
        "quote_posts", "quote_cancels", "closed_orders", "rest_seconds",
        "queue_consumed", "filled_shares", "action_seconds", "action_batches",
        "post_only_rejects", "pre_activation_trades", "taker_fees",
        "maker_rebates", "sell_pair_shares", "sell_pair_proceeds",
    }
    _MAX_METRICS = {
        "stale_market_events", "exposed_stale_market_events",
        "max_stale_event_lag_ms", "max_exposed_stale_event_lag_ms",
        "delayed_trade_events", "exposed_delayed_trade_events",
        "max_delayed_trade_lag_ms", "max_exposed_delayed_trade_lag_ms",
    }

    def __init__(
        self, config: PairConfig, asset: str, slug: str, start: int,
        up_token: str, down_token: str, observed_at: float | None = None,
    ) -> None:
        if config.mode != "accumulate" or len(config.ladder_offsets) < 2:
            raise ValueError("a ladder requires accumulate mode and at least two levels")
        lane_limit = config.max_inventory / len(config.ladder_offsets)
        if lane_limit + 1e-9 < config.clip_shares:
            raise ValueError("ladder inventory must fund one clip per level")
        self.config, self.asset, self.slug = config, asset, slug
        self.start, self.end = start, start + 300
        self.tokens = {True: up_token, False: down_token}
        self.lanes = tuple(
            _StableLane(
                dataclasses.replace(
                    config,
                    name=f"{config.name}_l{index}",
                    improve_ticks=offset,
                    max_inventory=lane_limit,
                    ladder_offsets=(),
                ),
                asset, slug, start, up_token, down_token, observed_at,
            )
            for index, offset in enumerate(config.ladder_offsets)
        )
        self.buy_pairs = PairLots()

    @property
    def full_window(self) -> bool:
        return all(lane.full_window for lane in self.lanes)

    @property
    def invalid_reason(self) -> str | None:
        return next((lane.invalid_reason for lane in self.lanes
                     if lane.invalid_reason is not None), None)

    @property
    def invalid_event_lag_ms(self) -> float | None:
        values = [lane.invalid_event_lag_ms for lane in self.lanes
                  if lane.invalid_event_lag_ms is not None]
        return max(values) if values else None

    @property
    def inventory(self) -> dict[bool, float]:
        return {side: sum(lane.inventory[side] for lane in self.lanes)
                for side in (True, False)}

    @property
    def cash(self) -> float:
        return sum(lane.cash for lane in self.lanes)

    @property
    def peak(self) -> float:
        return sum(lane.peak for lane in self.lanes)

    @property
    def buys(self) -> int:
        return sum(lane.buys for lane in self.lanes)

    @property
    def sells(self) -> int:
        return sum(lane.sells for lane in self.lanes)

    @property
    def orders(self) -> dict[tuple[int, bool, str], RestingOrder]:
        return {
            (index, side, order_side): order
            for index, lane in enumerate(self.lanes)
            for (side, order_side), order in lane.orders.items()
        }

    def _record_buys(
        self, now: float, records: list[dict[str, float | str]],
    ) -> None:
        for record in records:
            if record["action"] not in ("buy", "taker_buy"):
                continue
            shares = float(record["size"])
            net_price = -float(record["signed_cash"]) / shares
            side_up = bool(int(float(record["outcome_up"])))
            self.buy_pairs.add(side_up, shares, net_price, now)

    def on_books(
        self, now: float, up: OrderBook, down: OrderBook,
    ) -> list[dict[str, float | str]]:
        records = [record for lane in self.lanes
                   for record in lane.on_books(now, up, down)]
        self._record_buys(now, records)
        return records

    def on_trade(
        self, now: float, side_up: bool, price: float, size: float,
        taker_side: str, received_at: float | None = None,
    ) -> dict[str, float | str] | None:
        order_side = "buy" if taker_side.upper() == "SELL" else "sell"
        key = (side_up, order_side)
        lanes = [lane for lane in self.lanes if key in lane.orders]
        lanes.sort(
            key=lambda lane: lane.orders[key].price,
            reverse=order_side == "buy",
        )
        records: list[dict[str, float | str]] = []
        remaining = size
        for lane in lanes:
            if remaining <= 1e-9:
                break
            order = lane.orders.get(key)
            if order is None:
                continue
            queue_consumed = (
                min(order.queue_ahead, remaining)
                if price == order.price else 0.0
            )
            record = lane.on_trade(
                now, side_up, price, remaining, taker_side, received_at,
            )
            remaining -= queue_consumed
            if record is not None:
                remaining -= float(record["size"])
                records.append(record)
        self._record_buys(now, records)
        if not records:
            return None
        shares = sum(float(record["size"]) for record in records)
        signed_cash = sum(float(record["signed_cash"]) for record in records)
        weighted_price = round(sum(
            float(record["price"]) * float(record["size"]) for record in records
        ) / shares, 10)
        return {
            "action": records[0]["action"], "price": weighted_price,
            "size": shares, "signed_cash": signed_cash,
            "outcome_up": int(side_up),
        }

    def invalidate(
        self, now: float, reason: str = "feed_gap",
        event_lag_ms: float | None = None,
    ) -> None:
        for lane in self.lanes:
            lane.invalidate(now, reason, event_lag_ms)

    def observe_stale_market_event(self, event_lag_ms: float | None,
                                   event_at: float | None = None) -> None:
        for lane in self.lanes:
            lane.observe_stale_market_event(event_lag_ms, event_at)

    def observe_delayed_trade_event(self, event_lag_ms: float,
                                    event_at: float | None = None) -> None:
        for lane in self.lanes:
            lane.observe_delayed_trade_event(event_lag_ms, event_at)

    def settle(
        self, now: float, outcome_up: int,
    ) -> tuple[dict[str, float | int], dict[str, object]]:
        rows = [lane.settle(now, outcome_up) for lane in self.lanes]
        inventory = self.inventory
        settlement = {
            "cash": self.cash,
            "residual": inventory[True] * outcome_up
                        + inventory[False] * (1 - outcome_up),
            "pnl": sum(float(row[0]["pnl"]) for row in rows),
            "capital": self.peak,
            "buys": self.buys,
            "sells": self.sells,
            "resid_shares": sum(inventory.values()),
            "n_fills": self.buys + self.sells,
            "outcome_up": outcome_up,
        }
        metrics: dict[str, object] = {
            key: sum(_number(row[1], key) for row in rows)
            for key in self._SUM_METRICS
        }
        metrics.update({
            key: max(_number(row[1], key) for row in rows)
            for key in self._MAX_METRICS
        })
        metrics.update({
            "paired_end": min(inventory.values()),
            "unmatched_end": abs(inventory[True] - inventory[False]),
            "buy_pair_shares": self.buy_pairs.paired_shares,
            "buy_pair_cost": self.buy_pairs.paired_value,
            "buy_pair_delays": self.buy_pairs.completion_delays,
        })
        return settlement, metrics
