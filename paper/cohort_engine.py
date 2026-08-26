"""Synchronous queue-aware paper cohort state machine."""

from __future__ import annotations

import dataclasses
import math
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from typing import TypeAlias

from live.feed_health import (
    event_time_s,
    future_event_skew_s,
    market_event_tokens,
    stale_market_event,
)
from paper.ladder_engine import LadderWindow
from paper.market_metadata import ActiveMarket
from paper.order_book import OrderBookCache
from paper.pair_engine import PairWindow
from paper.pair_types import PairConfig
from paper.settlement import settle_valid

PaperWindow: TypeAlias = PairWindow | LadderWindow


@dataclasses.dataclass(frozen=True)
class FillRecord:
    ts: float
    strategy: str
    asset: str
    slug: str
    action: str
    price: float
    size: float
    signed_cash: float
    outcome_up: int


@dataclasses.dataclass(frozen=True)
class SettlementRecord:
    ts: float
    strategy: str
    asset: str
    slug: str
    cash: float
    residual: float
    pnl: float
    capital: float
    buys: int
    sells: int
    resid_shares: float
    n_fills: int
    outcome_up: int
    metrics: dict[str, object]


@dataclasses.dataclass(frozen=True)
class InvalidWindowRecord:
    ts: float
    strategy: str
    asset: str
    slug: str
    reason: str
    n_fills: int
    capital: float
    cash: float
    up_shares: float
    down_shares: float
    event_lag_ms: float | None


CohortRecord = FillRecord | SettlementRecord | InvalidWindowRecord


@dataclasses.dataclass
class _MarketCohort:
    market: ActiveMarket
    windows: dict[str, PaperWindow]
    finished_at: float | None = None


class CohortEngine:
    """Apply one ordered public feed to independent strategy counterfactuals."""

    def __init__(
        self, configs: Sequence[PairConfig], *, max_event_lag_s: float = 0.4,
    ) -> None:
        self.configs = tuple(configs)
        names = [config.name for config in self.configs]
        if (not names or any(not name for name in names)
                or len(names) != len(set(names))):
            raise ValueError("strategy names must be non-empty and unique")
        if max_event_lag_s <= 0:
            raise ValueError("max_event_lag_s must be positive")
        self.max_event_lag_s = max_event_lag_s
        self.books = OrderBookCache()
        self._active: dict[str, _MarketCohort] = {}
        self._finished: dict[str, deque[_MarketCohort]] = defaultdict(deque)
        self._token_map: dict[str, tuple[str, bool]] = {}
        self._fresh_tokens: set[str] = set()
        self._stale_assets: set[str] = set()

    def reset_feed(self) -> None:
        """Forget all book state until fresh snapshots arrive for active markets."""
        self.books.clear()
        self._fresh_tokens.clear()
        self._stale_assets.update(self._active)

    def runtime_snapshot(self) -> dict[str, object]:
        """Return operational counters without exposing mutable engine state."""
        fills = {
            config.name: sum(
                cohort.windows[config.name].buys
                + cohort.windows[config.name].sells
                for cohort in self._active.values()
            )
            for config in self.configs
        }
        orders = sum(
            len(window.orders)
            for cohort in self._active.values()
            for window in cohort.windows.values()
        )
        return {
            "fills": fills,
            "orders": orders,
            "stale_assets": sorted(self._stale_assets),
        }

    def open_market(self, market: ActiveMarket, observed_at: float) -> None:
        if market.asset in self._active:
            raise RuntimeError(f"active market already exists for {market.asset}")
        tokens = {market.up_token, market.down_token}
        if tokens & self._token_map.keys():
            raise ValueError(f"active token collision for {market.slug}")
        windows: dict[str, PaperWindow] = {}
        for config in self.configs:
            if config.ladder_offsets:
                window: PaperWindow = LadderWindow(
                    config, market.asset, market.slug, market.start,
                    market.up_token, market.down_token, observed_at,
                )
            else:
                window = PairWindow(
                    config, market.asset, market.slug, market.start,
                    market.up_token, market.down_token, observed_at,
                )
            windows[config.name] = window
        self.books.set_min_order_size(market.up_token, market.min_order_size)
        self.books.set_min_order_size(market.down_token, market.min_order_size)
        self._active[market.asset] = _MarketCohort(market, windows)
        self._token_map[market.up_token] = (market.asset, True)
        self._token_map[market.down_token] = (market.asset, False)
        self._fresh_tokens.difference_update(tokens)
        self._stale_assets.add(market.asset)

    def on_event(
        self, event: Mapping[str, object], known_at: float,
    ) -> tuple[FillRecord, ...]:
        event_tokens = market_event_tokens(event)
        if event_tokens and not event_tokens & self._token_map.keys():
            return ()
        event_type = event.get("event_type")
        if event_type in ("book", "price_change"):
            if not self._gate_freshness(event, known_at):
                return ()
            event_at = event_time_s(event)
            assert event_at is not None
            changed = self.books.apply(event, known_at, source_at=event_at)
            self._fresh_tokens.update(changed & self._token_map.keys())
            self._expire_freshness(known_at)
        else:
            self.books.apply(event, known_at)
        if event.get("event_type") != "last_trade_price":
            return ()
        token = str(event.get("asset_id") or "")
        info = self._token_map.get(token)
        taker_side = str(event.get("side") or "").upper()
        if info is None or taker_side not in ("BUY", "SELL"):
            return ()
        try:
            price = float(str(event["price"]))
            size = float(str(event["size"]))
        except (KeyError, TypeError, ValueError):
            return ()
        if (not math.isfinite(price) or not math.isfinite(size)
                or not 0 < price < 1 or size <= 0):
            return ()
        traded_at = event_time_s(event)
        if traded_at is None:
            return ()
        asset, side_up = info
        cohort = self._active.get(asset)
        if cohort is None:
            return ()
        clock_lead_s = future_event_skew_s(event, known_at)
        if clock_lead_s is not None:
            lead_ms = clock_lead_s * 1000
            for window in cohort.windows.values():
                window.invalidate(known_at, "future_trade_timestamp", lead_ms)
            return ()
        lag_ms = max(0.0, 1000 * (known_at - traded_at))
        if lag_ms > self.max_event_lag_s * 1000:
            for window in cohort.windows.values():
                window.observe_delayed_trade_event(lag_ms, traded_at)
        fills: list[FillRecord] = []
        for strategy, window in cohort.windows.items():
            record = window.on_trade(
                traded_at, side_up, price, size, taker_side,
                received_at=known_at,
            )
            if record is not None:
                fills.append(self._fill_record(
                    traded_at, strategy, asset, window.slug, record,
                ))
        return tuple(fills)

    def tick(self, now: float) -> tuple[FillRecord, ...]:
        self._expire_freshness(now)
        fills: list[FillRecord] = []
        for asset in sorted(self._active):
            if asset in self._stale_assets:
                continue
            cohort = self._active[asset]
            up = self.books.get(cohort.market.up_token)
            down = self.books.get(cohort.market.down_token)
            if up is None or down is None:
                continue
            for strategy, window in cohort.windows.items():
                for record in window.on_books(now, up, down):
                    fills.append(self._fill_record(
                        now, strategy, asset, window.slug, record,
                    ))
        return tuple(fills)

    def disconnect(self, now: float) -> None:
        for cohort in self._active.values():
            for window in cohort.windows.values():
                window.invalidate(now, "ws_reconnect")
        self.reset_feed()

    def finish_window(self, asset: str, end_at: float) -> None:
        cohort = self._active.get(asset)
        if cohort is None:
            raise RuntimeError(f"no active market for {asset}")
        if end_at + 1e-9 < cohort.market.start + 300:
            raise ValueError(f"cannot finish {cohort.market.slug} before its end")
        cohort.finished_at = end_at
        self._finished[asset].append(cohort)
        del self._active[asset]
        self._stale_assets.discard(asset)
        for token in (cohort.market.up_token, cohort.market.down_token):
            self._token_map.pop(token, None)
            self._fresh_tokens.discard(token)
            self.books.drop(token)

    def settle(
        self, asset: str, outcome_up: int, observed_at: float, *,
        slug: str | None = None,
    ) -> tuple[SettlementRecord | InvalidWindowRecord, ...]:
        if outcome_up not in (0, 1):
            raise ValueError("outcome_up must be zero or one")
        pending = self._finished.get(asset)
        if not pending:
            raise RuntimeError(f"no finished market for {asset}")
        cohort = pending[0] if slug is None else next(
            (candidate for candidate in pending if candidate.market.slug == slug), None,
        )
        if cohort is None:
            raise RuntimeError(f"no finished market {slug!r} for {asset}")
        assert cohort.finished_at is not None
        if observed_at + 1e-9 < cohort.finished_at:
            raise ValueError("resolution cannot precede the finished market")
        pending.remove(cohort)
        if not pending:
            del self._finished[asset]
        scored, skipped = settle_valid(cohort.windows, observed_at, outcome_up)
        records: list[SettlementRecord | InvalidWindowRecord] = [
            InvalidWindowRecord(
                observed_at, row.strategy, asset, row.slug, row.reason,
                row.n_fills, row.capital, row.cash, row.up_shares,
                row.down_shares, row.event_lag_ms,
            )
            for row in skipped
        ]
        for row in scored:
            values = row.settlement
            records.append(SettlementRecord(
                observed_at, row.strategy, asset, cohort.market.slug,
                float(values["cash"]), float(values["residual"]),
                float(values["pnl"]), float(values["capital"]),
                int(values["buys"]), int(values["sells"]),
                float(values["resid_shares"]), int(values["n_fills"]),
                int(values["outcome_up"]), row.metrics,
            ))
        return tuple(records)

    def _expire_freshness(self, now: float) -> None:
        for token in tuple(self._fresh_tokens):
            book = self.books.get(token)
            if (book is None or not book.bootstrapped
                    or now < book.received_at
                    or now - book.received_at > self.max_event_lag_s):
                self._fresh_tokens.discard(token)
        for asset, cohort in self._active.items():
            required = {cohort.market.up_token, cohort.market.down_token}
            if required <= self._fresh_tokens:
                self._stale_assets.discard(asset)
            else:
                self._stale_assets.add(asset)

    def _gate_freshness(
        self, event: Mapping[str, object], known_at: float,
    ) -> bool:
        if event.get("event_type") not in ("book", "price_change"):
            return True
        tokens = market_event_tokens(event) & self._token_map.keys()
        assets = sorted({self._token_map[token][0] for token in tokens})
        if not assets:
            return False
        event_at = event_time_s(event)
        stale = stale_market_event(event, known_at, self.max_event_lag_s)
        clock_lead_s = future_event_skew_s(event, known_at)
        event_lag_ms = (
            None if event_at is None else max(0.0, 1000 * (known_at - event_at))
        )
        if stale:
            self._fresh_tokens.difference_update(tokens)
        for asset in assets:
            cohort = self._active.get(asset)
            if cohort is None:
                continue
            if stale:
                self._stale_assets.add(asset)
                for window in cohort.windows.values():
                    if clock_lead_s is not None:
                        window.invalidate(
                            known_at, "future_market_timestamp",
                            clock_lead_s * 1000,
                        )
                    else:
                        window.observe_stale_market_event(event_lag_ms, event_at)
        return not stale

    @staticmethod
    def _fill_record(
        ts: float, strategy: str, asset: str, slug: str,
        record: Mapping[str, float | str],
    ) -> FillRecord:
        return FillRecord(
            ts, strategy, asset, slug, str(record["action"]),
            float(record["price"]), float(record["size"]),
            float(record["signed_cash"]), int(float(record["outcome_up"])),
        )
