"""Focused, queue-aware maker inventory paper runner. No keys or orders."""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
import logging
import math
import os
import time

import websockets

from live.feed_health import (
    FeedHealth,
    MARKET_WS_MAX_QUEUE,
    event_time_s,
    market_event_tokens,
    stale_market_event,
)
from live.window_clock import boundary_aligned_delay
from paper import envload, report
from live.feed_pump import (
    FeedPump,
    FeedPumpStats,
    WebSocketLike,
    subscription_messages,
)
from live.loop_health import EventLoopHealth
from paper.ladder_engine import LadderWindow
from paper.ledger_writer import LedgerWriter
from paper.market_metadata import ActiveMarket, fetch_active_market
from paper.notify import notifier
from paper.order_book import OrderBookCache
from paper.pair_engine import PairWindow
from paper.pair_types import PairConfig
from paper.reference_feed import ReferenceFeed, ReferenceUpdate
from paper.settlement import settle_valid
from tools.market_windows import ASSET_PREFIX, fetch_gamma_window

PaperWindow = PairWindow | LadderWindow

envload.load()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("paper.run")

ACTION_LATENCY_S = float(os.environ.get("PAPER_ACTION_LATENCY_MS", "65")) / 1000
if not 0.01 <= ACTION_LATENCY_S <= 0.5:
    raise RuntimeError("PAPER_ACTION_LATENCY_MS must be between 10 and 500")
MAX_MARKET_EVENT_LAG_S = float(
    os.environ.get("PAPER_MAX_EVENT_LAG_MS", "400")
) / 1000
if not 0.05 <= MAX_MARKET_EVENT_LAG_S <= 5:
    raise RuntimeError("PAPER_MAX_EVENT_LAG_MS must be between 50 and 5000")
DECISION_CADENCE_S = 0.01

ASSETS = dict(ASSET_PREFIX)
requested = os.environ.get("PAPER_ASSETS")
if requested:
    wanted = {item.strip() for item in requested.split(",")}
    ASSETS = {asset: prefix for asset, prefix in ASSETS.items() if asset in wanted}

STRATEGIES = (
    PairConfig("basket99", "accumulate", 0.02,
               action_latency_s=ACTION_LATENCY_S, buy_sum_ceiling=0.99,
               improve_ticks=1, require_both_to_start=True, basket_average_cap=True,
               new_pair_start_s=30),
    PairConfig("basket99t270d", "accumulate", 0.02,
               action_latency_s=ACTION_LATENCY_S, buy_sum_ceiling=0.99,
               improve_ticks=1, require_both_to_start=True, basket_average_cap=True,
               new_pair_start_s=30, buy_taker_after_s=270,
               taker_dust_round_shares=0.1),
    PairConfig("basket99dust10", "accumulate", 0.02,
               action_latency_s=ACTION_LATENCY_S, buy_sum_ceiling=0.99,
               improve_ticks=1, require_both_to_start=True, basket_average_cap=True,
               new_pair_start_s=30, balanced_dust_shares=0.1),
    PairConfig("mintcycle5", "mint", 0.5,
               action_latency_s=ACTION_LATENCY_S, mint_sets=5,
               sell_sum_floor=1.005, new_pair_start_s=30,
               new_pair_cutoff_s=240, mint_anchor_spread=0.02),
    PairConfig("mintrepair5p100", "mint", 0.5,
               action_latency_s=ACTION_LATENCY_S, mint_sets=5,
               sell_sum_floor=1.005, new_pair_start_s=30,
               new_pair_cutoff_s=240, mint_anchor_spread=0.02,
               taker_hedge_after_s=60, taker_pair_sum_floor=1.0,
               taker_dust_round_shares=0.1),
)
MKT_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
KILL = "paper/KILL"


class PaperKill(Exception):
    pass


@dataclasses.dataclass
class PendingWindow:
    asset: str
    start: int
    windows: dict[str, PaperWindow]


class State:
    def __init__(self) -> None:
        self.active: dict[str, dict[str, PaperWindow]] = {}
        self.pending: list[PendingWindow] = []
        self.books = OrderBookCache()
        self.token_map: dict[str, tuple[str, bool]] = {}
        self.tokens: set[str] = set()
        self.tokens_changed = asyncio.Event()
        self.ledger = LedgerWriter()
        self.notify = notifier()
        self.events: collections.Counter[str] = collections.Counter()
        self.resolution_errors: collections.Counter[str] = collections.Counter()
        self.feed_health = FeedHealth()
        self.feed_pump: FeedPump | None = None
        self.feed_pump_stats = FeedPumpStats()
        self.loop_health = EventLoopHealth()
        self.fresh_tokens: set[str] = set()
        self.stale_assets: set[str] = set()
        self.reference_feed = ReferenceFeed(ASSETS)


S = State()


def _record_reference(update: ReferenceUpdate) -> None:
    S.ledger.record_reference(
        update.asset, update.observed_at, update.received_at,
        update.value_e18, update.window_s,
    )


def _new_windows(market: ActiveMarket, observed_at: float) -> dict[str, PaperWindow]:
    windows: dict[str, PaperWindow] = {}
    for config in STRATEGIES:
        window_type = LadderWindow if config.ladder_offsets else PairWindow
        windows[config.name] = window_type(
            config, market.asset, market.slug, market.start,
            market.up_token, market.down_token, observed_at,
        )
    return windows


def _refresh_tokens() -> None:
    old_tokens = S.tokens
    token_map: dict[str, tuple[str, bool]] = {}
    for asset, windows in S.active.items():
        if not windows:
            continue
        sample = next(iter(windows.values()))
        token_map[sample.tokens[True]] = (asset, True)
        token_map[sample.tokens[False]] = (asset, False)
    S.token_map = token_map
    S.tokens = set(token_map)
    S.fresh_tokens.intersection_update(S.tokens)
    S.stale_assets = {
        asset for asset, windows in S.active.items()
        if windows and not set(next(iter(windows.values())).tokens.values()) <= S.fresh_tokens
    }
    for token in old_tokens - S.tokens:
        S.books.drop(token)
    if S.tokens != old_tokens:
        S.tokens_changed.set()


async def _discover(asset: str, base: int) -> tuple[str, ActiveMarket | None]:
    try:
        return asset, await asyncio.to_thread(fetch_active_market, asset, base)
    except RuntimeError as exc:
        log.warning("market discovery %s: %s", asset, exc)
        return asset, None


async def window_task() -> None:
    current_base = -1
    while True:
        base = int(time.time() // 300) * 300
        if base != current_base:
            for asset, windows in list(S.active.items()):
                if windows:
                    start = next(iter(windows.values())).start
                    if start < base:
                        S.pending.append(PendingWindow(asset, start, windows))
                        S.active[asset] = {}
            current_base = base
        missing = [asset for asset in ASSETS if not S.active.get(asset)]
        if missing:
            discoveries = await asyncio.gather(*(_discover(asset, base) for asset in missing))
            for asset, market in discoveries:
                if market is not None:
                    S.books.set_min_order_size(market.up_token, market.min_order_size)
                    S.books.set_min_order_size(market.down_token, market.min_order_size)
                    S.active[asset] = _new_windows(market, time.time())
                    log.info("opened %s %s with %d focused strategies", asset, market.slug,
                             len(STRATEGIES))
            _refresh_tokens()
        await asyncio.sleep(boundary_aligned_delay(time.time()))


async def settlement_task() -> None:
    while True:
        await asyncio.sleep(15)
        for pending in list(S.pending):
            try:
                resolved = await asyncio.to_thread(
                    fetch_gamma_window, pending.asset, pending.start, 1
                )
            except RuntimeError as exc:
                S.resolution_errors[pending.asset] += 1
                log.warning("resolution fetch %s-%d: %s", pending.asset, pending.start, exc)
                continue
            if resolved is None:
                continue
            now = time.time()
            S.ledger.record_resolved_window(
                now, pending.asset, resolved.slug, resolved.winner_up,
            )
            scored, skipped = settle_valid(pending.windows, now, resolved.winner_up)
            for skipped_row in skipped:
                S.ledger.record_invalid_window(
                    now, skipped_row.strategy, pending.asset, skipped_row.slug,
                    {
                        "reason": skipped_row.reason,
                        "n_fills": skipped_row.n_fills,
                        "capital": skipped_row.capital,
                        "cash": skipped_row.cash,
                        "up_shares": skipped_row.up_shares,
                        "down_shares": skipped_row.down_shares,
                        "event_lag_ms": skipped_row.event_lag_ms,
                    },
                )
                log.info("skipped invalid strategy-window %s %s reason=%s",
                         skipped_row.strategy, skipped_row.slug, skipped_row.reason)
            for scored_row in scored:
                window = pending.windows[scored_row.strategy]
                S.ledger.record_settlement(
                    now, scored_row.strategy, pending.asset, window.slug,
                    scored_row.settlement,
                )
                S.ledger.record_metrics(
                    now, scored_row.strategy, pending.asset, window.slug,
                    scored_row.metrics,
                )
            S.pending.remove(pending)
            log.info("officially resolved %s outcome=%s scored=%d/%d", resolved.slug,
                     "Up" if resolved.winner_up else "Down", len(scored),
                     len(pending.windows))


def _quote_windows(asset: str, now: float) -> None:
    if asset in S.stale_assets:
        return
    windows = S.active.get(asset) or {}
    if not windows:
        return
    sample = next(iter(windows.values()))
    up, down = S.books.get(sample.tokens[True]), S.books.get(sample.tokens[False])
    if up is None or down is None:
        return
    for name, window in windows.items():
        for fill in window.on_books(now, up, down):
            S.ledger.record_fill(now, name, asset, window.slug, fill)


def _affected_assets(tokens: set[str]) -> set[str]:
    return {S.token_map[token][0] for token in tokens if token in S.token_map}


def _gate_stale_market_event(event: dict[str, object], now: float) -> None:
    if event.get("event_type") not in ("book", "price_change"):
        return
    tokens = market_event_tokens(event) & S.tokens
    assets = _affected_assets(tokens)
    if not assets:
        return
    event_at = event_time_s(event)
    stale = stale_market_event(event, now, MAX_MARKET_EVENT_LAG_S)
    if event_at is None:
        missing_key = f"{event.get('event_type')}_missing_timestamp"
        S.events[missing_key] += 1
        if S.events[missing_key] in (1, 10, 100, 1_000, 10_000):
            log.warning("market event missing timestamp type=%s count=%d keys=%s",
                        event.get("event_type"), S.events[missing_key],
                        sorted(event))
    event_lag_ms = None if event_at is None else max(0.0, 1000 * (now - event_at))
    if stale:
        S.events["stale_market_event"] += 1
        S.fresh_tokens.difference_update(tokens)
    else:
        S.fresh_tokens.update(tokens)
    for asset in assets:
        windows = S.active.get(asset) or {}
        required = set(next(iter(windows.values())).tokens.values()) if windows else set()
        if required <= S.fresh_tokens:
            S.stale_assets.discard(asset)
        else:
            S.stale_assets.add(asset)
        if not stale:
            continue
        for window in windows.values():
            window.observe_stale_market_event(event_lag_ms, event_at)


def handle_event(event: dict[str, object]) -> None:
    event_type = str(event.get("event_type") or "?")
    S.events[event_type] += 1
    now = time.time()
    S.feed_health.observe(event, now)
    event_tokens = market_event_tokens(event)
    if event_tokens and not event_tokens & S.tokens:
        S.events["retired_market_event"] += 1
        return
    S.books.apply(event, now)
    _gate_stale_market_event(event, now)
    if event_type != "last_trade_price":
        return
    token = str(event.get("asset_id") or "")
    info = S.token_map.get(token)
    taker_side = str(event.get("side") or "").upper()
    if info is None or taker_side not in ("BUY", "SELL"):
        return
    try:
        price, size = float(str(event["price"])), float(str(event["size"]))
    except (KeyError, TypeError, ValueError):
        return
    if (not math.isfinite(price) or not math.isfinite(size)
            or not 0 < price < 1 or size <= 0):
        return
    asset, side_up = info
    traded_at = event_time_s(event)
    if traded_at is None:
        S.events["last_trade_missing_timestamp"] += 1
        return
    trade_lag_ms = max(0.0, 1000 * (now - traded_at))
    if trade_lag_ms > MAX_MARKET_EVENT_LAG_S * 1000:
        S.events["delayed_trade_event"] += 1
        for window in (S.active.get(asset) or {}).values():
            window.observe_delayed_trade_event(trade_lag_ms, traded_at)
    for name, window in (S.active.get(asset) or {}).items():
        fill = window.on_trade(
            traded_at, side_up, price, size, taker_side, received_at=now,
        )
        if fill:
            S.ledger.record_fill(traded_at, name, asset, window.slug, fill)


async def _rotate_subscriptions(
    ws: WebSocketLike, subscribed: set[str],
) -> None:
    while True:
        await S.tokens_changed.wait()
        S.tokens_changed.clear()
        target = set(S.tokens)
        for message in subscription_messages(subscribed, target):
            await ws.send(json.dumps(message))
        if target != subscribed:
            log.info("market ws rotated %d -> %d tokens",
                     len(subscribed), len(target))
        subscribed = target


async def market_task() -> None:
    retry_delay = 0.1
    while True:
        while not S.tokens:
            S.tokens_changed.clear()
            if not S.tokens:
                await S.tokens_changed.wait()
        tokens = set(S.tokens)
        connected_at: float | None = None
        try:
            S.books.clear()
            async with websockets.connect(
                MKT_WS, ping_interval=None, open_timeout=12, close_timeout=0.1,
                max_queue=MARKET_WS_MAX_QUEUE,
            ) as ws:
                connected_at = time.monotonic()
                await ws.send(json.dumps({
                    "assets_ids": sorted(tokens), "type": "market",
                }))
                log.info("market ws subscribed %d tokens", len(tokens))
                S.feed_pump = FeedPump(handle_event, stats=S.feed_pump_stats)
                connection_stop = asyncio.Event()
                pump_task = asyncio.create_task(S.feed_pump.run(ws, connection_stop))
                rotate_task = asyncio.create_task(_rotate_subscriptions(ws, tokens))
                tasks = {pump_task, rotate_task}
                try:
                    done, _ = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        task.result()
                finally:
                    connection_stop.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            if connected_at is not None:
                S.feed_health.reconnect()
                now = time.time()
                for windows in S.active.values():
                    for window in windows.values():
                        window.invalidate(now, "ws_reconnect")
            if connected_at is not None and time.monotonic() - connected_at >= 5:
                retry_delay = 0.1
            wait = retry_delay
            retry_delay = min(2.0, retry_delay * 2)
            log.warning("market ws reconnect in %.1fs: %s: %s",
                        wait, exc.__class__.__name__, exc)
            await asyncio.sleep(wait)


async def quote_task() -> None:
    """Poll after feed bursts so post-trade deltas cannot erase prior fills."""
    while True:
        now = time.time()
        for asset in S.active:
            _quote_windows(asset, now)
        await asyncio.sleep(DECISION_CADENCE_S)


async def heartbeat_task() -> None:
    while True:
        await asyncio.sleep(20)
        active = {
            name: sum(window.buys + window.sells for windows in S.active.values()
                      for candidate, window in windows.items() if candidate == name)
            for name in (config.name for config in STRATEGIES)
        }
        orders = sum(len(window.orders) for windows in S.active.values()
                     for window in windows.values())
        feed_queue = S.feed_pump_stats.snapshot(reset_interval=True)
        log.info("hb | events=%s fills=%s active_orders=%d pending_resolution=%d "
                 "feed_paused=%s feed=%s feed_queue=%s loop=%s reference=%s errors=%s",
                 dict(S.events), active, orders, len(S.pending),
                 sorted(S.stale_assets),
                 S.feed_health.snapshot(reset_interval=True), feed_queue,
                 S.loop_health.snapshot(reset_interval=True),
                 S.reference_feed.snapshot(),
                 dict(S.resolution_errors))


async def report_task() -> None:
    delay = 120.0
    interval = float(os.environ.get("PAPER_SUMMARY_MINS", "15")) * 60
    while True:
        await asyncio.sleep(delay)
        delay = interval
        text = report.text()
        for line in text.splitlines():
            log.info(line)
        await asyncio.to_thread(S.notify.send, report.tg_text(), pre=True)


async def kill_task() -> None:
    while not os.path.exists(KILL):
        await asyncio.sleep(1)
    raise PaperKill("paper KILL present")


async def main() -> None:
    if os.path.exists(KILL):
        raise SystemExit("paper KILL present")
    names = [config.name for config in STRATEGIES]
    log.info("focused pair paper starting | strategies=%s | queue-ahead fills | "
             "action-latency=%dms | max-market-lag=%dms | decision cadence=%dms | "
             "official Gamma outcomes | assets=%s",
             names, round(ACTION_LATENCY_S * 1000),
             round(MAX_MARKET_EVENT_LAG_S * 1000),
             round(DECISION_CADENCE_S * 1000), list(ASSETS))
    try:
        await asyncio.gather(window_task(), settlement_task(), market_task(),
                             S.reference_feed.run(_record_reference), quote_task(),
                             S.loop_health.run(), heartbeat_task(), report_task(),
                             kill_task(),
                             asyncio.to_thread(
                                 S.notify.send,
                                 f"focused pair paper started ({len(names)} strategies, "
                                 "queue-aware, no orders)",
                             ))
    finally:
        await asyncio.to_thread(S.ledger.close)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except PaperKill:
        log.info("stopped by paper KILL")
    except KeyboardInterrupt:
        log.info("stopped")
