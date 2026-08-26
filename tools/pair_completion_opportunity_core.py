"""I/O-free competing-event census for unmatched basket buy legs."""

from __future__ import annotations

import copy
import dataclasses

from paper.buy_completion import plan_buy_completion
from paper.cohort_engine import CohortEngine, FillRecord
from paper.order_book import OrderBook
from paper.pair_engine import PairWindow
from paper.pair_types import PairConfig


@dataclasses.dataclass(frozen=True)
class PairState:
    side_up: bool | None
    shares: float
    average_price: float | None


@dataclasses.dataclass(frozen=True)
class Episode:
    episode_id: int
    asset: str
    slug: str
    side_up: bool
    opened_event_at: float
    opened_known_at: float
    initial_shares: float
    initial_average_price: float


def _state(window: PairWindow) -> PairState:
    side = window.buy_pairs.open_side
    if side is None:
        return PairState(None, 0.0, None)
    lots = window.buy_pairs.lots[side]
    shares = sum(lot.shares for lot in lots)
    if shares <= 0:
        raise RuntimeError("open pair side has no shares")
    return PairState(
        side, shares, sum(lot.shares * lot.price for lot in lots) / shares,
    )


def _window(engine: CohortEngine, asset: str) -> PairWindow:
    cohort = engine._active.get(asset)
    if cohort is None:
        raise RuntimeError(f"no active census market for {asset}")
    window = cohort.windows.get("basket99")
    if not isinstance(window, PairWindow):
        raise RuntimeError("census baseline is not a PairWindow")
    return window


class OpportunityCensus:
    def __init__(self, completion_config: PairConfig) -> None:
        self.completion_config = completion_config
        self.active: dict[str, Episode] = {}
        self.rows: list[dict[str, object]] = []
        self._next_id = 1

    def _start(
        self, window: PairWindow, state: PairState, fill: FillRecord, known_at: float,
    ) -> None:
        if state.side_up is None or state.average_price is None:
            raise RuntimeError("cannot start a balanced first-leg episode")
        if window.asset in self.active:
            raise RuntimeError(f"overlapping first-leg episode for {window.asset}")
        self.active[window.asset] = Episode(
            self._next_id, window.asset, window.slug, state.side_up,
            fill.ts, known_at, state.shares, state.average_price,
        )
        self._next_id += 1

    @staticmethod
    def _base(
        episode: Episode, endpoint: str, event_at: float, known_at: float,
        state: PairState,
    ) -> dict[str, object]:
        return {
            **dataclasses.asdict(episode),
            "endpoint": endpoint,
            "endpoint_event_at": event_at,
            "endpoint_known_at": known_at,
            "known_seconds_to_endpoint": known_at - episode.opened_known_at,
            "open_shares_at_endpoint": state.shares,
            "open_average_price_at_endpoint": state.average_price,
            "censor_reason": None,
            "natural_fill_price": None,
            "natural_fill_shares": None,
            "opportunity": None,
        }

    def after_fill(
        self, window: PairWindow, before: PairState, after: PairState,
        fill: FillRecord, known_at: float,
    ) -> None:
        if fill.action != "buy":
            raise RuntimeError(f"unexpected census fill action {fill.action!r}")
        if before.side_up is None and after.side_up is not None:
            self._start(window, after, fill, known_at)
            return
        if before.side_up is None or after.side_up == before.side_up:
            return
        episode = self.active.pop(window.asset, None)
        if episode is not None:
            row = self._base(
                episode, "natural_maker_completion", fill.ts, known_at, before,
            )
            row["natural_fill_price"] = fill.price
            row["natural_fill_shares"] = fill.size
            row["reentry_state_enabled"] = (
                after.side_up is None
                and known_at + window.config.action_latency_s
                < window.start + window.config.new_pair_cutoff_s
            )
            self.rows.append(row)
        if after.side_up is not None:
            self._start(window, after, fill, known_at)

    def observe_tick(
        self, window: PairWindow, up: OrderBook, down: OrderBook, now: float,
    ) -> bool:
        episode = self.active.get(window.asset)
        if episode is None:
            return False
        config = self.completion_config
        plan = plan_buy_completion(
            now - window.start,
            None if window.buy_opened_at is None else window.buy_opened_at - window.start,
            config, window.buy_pairs, window.inventory, up, down,
        )
        if plan is None:
            return False
        state = _state(window)
        shares = sum(leg.shares for leg in plan.legs)
        notional = sum(leg.price * leg.shares for leg in plan.legs)
        fees = sum(leg.fee for leg in plan.legs)
        copied_pairs = copy.deepcopy(window.buy_pairs)
        for leg in plan.legs:
            copied_pairs.add(
                plan.side, leg.shares,
                (leg.price * leg.shares + leg.fee) / leg.shares, now,
            )
        if copied_pairs.paired_shares <= 0:
            raise RuntimeError("completion opportunity produced no paired shares")
        order = window.orders.get((plan.side, "buy"))
        reference_reason: str | None = None
        maker_price: float | None = None
        if order is None:
            reference_reason = "no_resting_opposite_maker_order"
        elif order.size + 1e-9 < shares:
            reference_reason = "resting_opposite_maker_size_below_sweep"
        else:
            maker_price = order.price
        post_inventory = dict(window.inventory)
        post_inventory[plan.side] += shares
        clears = copied_pairs.open_side is None
        if not clears:
            return False
        reentry_enabled = (
            now + window.config.action_latency_s
            < window.start + window.config.new_pair_cutoff_s
            and abs(post_inventory[True] - post_inventory[False]) <= 0.1 + 1e-9
            and all(
                post_inventory[side] + window.config.clip_shares
                <= window.config.max_inventory + 1e-9
                for side in (True, False)
            )
        )
        row = self._base(episode, "taker_completion_opportunity", now, now, state)
        row["opportunity"] = {
            "hedge_side_up": plan.side,
            "shares": shares,
            "sweep_vwap": notional / shares,
            "taker_fee_usd": fees,
            "fee_inclusive_sweep_price": (notional + fees) / shares,
            "cumulative_pair_average_after": (
                copied_pairs.paired_value / copied_pairs.paired_shares
            ),
            "resting_maker_reference_price": maker_price,
            "maker_reference_unavailable_reason": reference_reason,
            "spread_cross_cost_usd": (
                None if maker_price is None else notional - maker_price * shares
            ),
            "fee_plus_spread_insurance_cost_usd": (
                None if maker_price is None
                else notional + fees - maker_price * shares
            ),
            "clears_open_pair": clears,
            "reentry_state_enabled": reentry_enabled,
        }
        row["reentry_state_enabled"] = reentry_enabled
        self.rows.append(row)
        del self.active[window.asset]
        return True

    def censor(
        self, asset: str, event_at: float, known_at: float, reason: str,
        window: PairWindow,
    ) -> None:
        episode = self.active.pop(asset, None)
        if episode is None:
            return
        row = self._base(episode, "censored", event_at, known_at, _state(window))
        row["censor_reason"] = reason
        row["reentry_state_enabled"] = None
        self.rows.append(row)
