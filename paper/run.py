"""Focused, queue-aware maker inventory paper runner. No keys or orders."""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
import logging
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
from paper.capture import PaperCapture
from live.feed_pump import (
    FeedPump,
    FeedPumpStats,
    WebSocketLike,
    subscription_messages,
)
from live.loop_health import EventLoopHealth
from paper.cohort_engine import (
    CohortEngine,
    CohortRecord,
    FillRecord,
    InvalidWindowRecord,
    SettlementRecord,
)
from paper.ledger_writer import LedgerWriter
from paper.live_gate import LiveGate
from paper.market_metadata import ActiveMarket, fetch_active_market
from paper.notify import notifier
from paper.reference_feed import ReferenceFeed, ReferenceUpdate
from paper.strategy_board import (
    current_strategy_board,
    preopen_strategy_board,
    strategy_board_hash,
)
from tools.market_windows import ASSET_PREFIX, fetch_gamma_window
from tools.transport_telemetry import CaptureWriteError

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

PREOPEN_TARGET_RAW = os.environ.get("PAPER_PREOPEN_TARGET_START")
PREOPEN_TARGET_START: int | None = None
if PREOPEN_TARGET_RAW:
    try:
        PREOPEN_TARGET_START = int(PREOPEN_TARGET_RAW)
    except ValueError as exc:
        raise RuntimeError("PAPER_PREOPEN_TARGET_START must be an epoch second") from exc
    if PREOPEN_TARGET_START <= 0 or PREOPEN_TARGET_START % 300:
        raise RuntimeError("PAPER_PREOPEN_TARGET_START must be a positive 5-minute boundary")

STRATEGIES = (
    preopen_strategy_board(ACTION_LATENCY_S)
    if PREOPEN_TARGET_START is not None
    else current_strategy_board(ACTION_LATENCY_S)
)
MKT_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
KILL = "paper/KILL"


class PaperKill(Exception):
    pass


@dataclasses.dataclass
class PendingWindow:
    asset: str
    start: int
    slug: str


class State:
    def __init__(self) -> None:
        self.markets: dict[str, ActiveMarket] = {}
        self.pending: list[PendingWindow] = []
        self.engine = CohortEngine(
            STRATEGIES, max_event_lag_s=MAX_MARKET_EVENT_LAG_S,
        )
        self.tokens: set[str] = set()
        self.tokens_changed = asyncio.Event()
        self.ledger = LedgerWriter()
        self.notify = notifier()
        self.events: collections.Counter[str] = collections.Counter()
        self.resolution_errors: collections.Counter[str] = collections.Counter()
        self.feed_health = FeedHealth()
        self.live_gate = LiveGate()
        self.feed_pump: FeedPump | None = None
        self.feed_pump_stats = FeedPumpStats()
        self.loop_health = EventLoopHealth()
        self.reference_feed = ReferenceFeed(ASSETS)
        self.capture: PaperCapture | None = None


S = State()


def _record_reference(update: ReferenceUpdate) -> None:
    # Publish on RECEIPT, not on the timestamp the sample describes. Every
    # sample arrives late (median 1.678s on this feed), and both review seats
    # called pricing a decision at observed_at textbook lookahead.
    try:
        S.engine.reference_view.update(
            update.asset, float(update.observed_at), int(update.value_e18) / 1e18)
    except (ValueError, TypeError, AttributeError):
        pass
    S.ledger.record_reference(
        update.asset, update.observed_at, update.received_at,
        update.value_e18, update.window_s,
    )


def _refresh_tokens() -> None:
    old_tokens = S.tokens
    S.tokens = {
        token
        for market in S.markets.values()
        for token in (market.up_token, market.down_token)
    }
    if S.tokens != old_tokens:
        S.tokens_changed.set()


def _record_cohort(records: tuple[CohortRecord, ...]) -> None:
    for record in records:
        if isinstance(record, FillRecord):
            S.ledger.record_fill(
                record.ts, record.strategy, record.asset, record.slug,
                {
                    "action": record.action,
                    "price": record.price,
                    "size": record.size,
                    "signed_cash": record.signed_cash,
                    "outcome_up": record.outcome_up,
                },
            )
        elif isinstance(record, InvalidWindowRecord):
            S.ledger.record_invalid_window(
                record.ts, record.strategy, record.asset, record.slug,
                {
                    "reason": record.reason,
                    "n_fills": record.n_fills,
                    "capital": record.capital,
                    "cash": record.cash,
                    "up_shares": record.up_shares,
                    "down_shares": record.down_shares,
                    "event_lag_ms": record.event_lag_ms,
                },
            )
            log.info(
                "skipped invalid strategy-window %s %s reason=%s",
                record.strategy, record.slug, record.reason,
            )
        elif isinstance(record, SettlementRecord):
            S.ledger.record_settlement(
                record.ts, record.strategy, record.asset, record.slug,
                {
                    "cash": record.cash,
                    "residual": record.residual,
                    "pnl": record.pnl,
                    "capital": record.capital,
                    "buys": record.buys,
                    "sells": record.sells,
                    "resid_shares": record.resid_shares,
                    "n_fills": record.n_fills,
                    "outcome_up": record.outcome_up,
                },
            )
            S.ledger.record_metrics(
                record.ts, record.strategy, record.asset, record.slug,
                record.metrics,
            )
        else:  # pragma: no cover - closed union, defensive runtime guard
            raise TypeError(f"unsupported cohort record {type(record).__name__}")


async def _discover(asset: str, base: int) -> tuple[str, ActiveMarket | None]:
    try:
        return asset, await asyncio.to_thread(fetch_active_market, asset, base)
    except RuntimeError as exc:
        log.warning("market discovery %s: %s", asset, exc)
        return asset, None


async def window_task() -> None:
    if PREOPEN_TARGET_START is not None:
        while True:
            now = time.time()
            finished_any = False
            for asset, active_market in list(S.markets.items()):
                if now + 1e-9 < active_market.start + 300:
                    continue
                observed_at = now
                S.engine.finish_window(asset, observed_at)
                if S.capture is not None:
                    S.capture.market_finish(
                        asset, active_market.slug, active_market.start, observed_at,
                    )
                S.pending.append(PendingWindow(
                    asset, active_market.start, active_market.slug,
                ))
                del S.markets[asset]
                finished_any = True
            if finished_any:
                _refresh_tokens()
            if now < PREOPEN_TARGET_START + 300:
                missing = [asset for asset in ASSETS if asset not in S.markets]
                if missing:
                    discoveries = await asyncio.gather(*(
                        _discover(asset, PREOPEN_TARGET_START) for asset in missing
                    ))
                    for asset, market in discoveries:
                        if market is None:
                            continue
                        observed_at = time.time()
                        S.engine.open_market(market, observed_at)
                        S.markets[asset] = market
                        if S.capture is not None:
                            S.capture.market_open(market, observed_at)
                        log.info(
                            "opened preopen target %s %s with %d focused strategies",
                            asset, market.slug, len(STRATEGIES),
                        )
                    _refresh_tokens()
            await asyncio.sleep(boundary_aligned_delay(time.time()))

    current_base = -1
    while True:
        base = int(time.time() // 300) * 300
        if base != current_base:
            for asset, active_market in list(S.markets.items()):
                if active_market.start < base:
                    observed_at = time.time()
                    S.engine.finish_window(asset, observed_at)
                    if S.capture is not None:
                        S.capture.market_finish(
                            asset, active_market.slug, active_market.start, observed_at,
                        )
                    S.pending.append(
                        PendingWindow(
                            asset, active_market.start, active_market.slug,
                        ),
                    )
                    del S.markets[asset]
            current_base = base
        missing = [asset for asset in ASSETS if asset not in S.markets]
        if missing:
            discoveries = await asyncio.gather(*(_discover(asset, base) for asset in missing))
            for asset, market in discoveries:
                if market is not None:
                    observed_at = time.time()
                    S.engine.open_market(market, observed_at)
                    S.markets[asset] = market
                    if S.capture is not None:
                        S.capture.market_open(market, observed_at)
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
            if S.capture is not None:
                S.capture.resolution(resolved, now)
            S.ledger.record_resolved_window(
                now, pending.asset, resolved.slug, resolved.winner_up,
            )
            records = S.engine.settle(
                pending.asset, resolved.winner_up, now, slug=pending.slug,
            )
            _record_cohort(records)
            S.pending.remove(pending)
            scored = sum(isinstance(row, SettlementRecord) for row in records)
            log.info("officially resolved %s outcome=%s scored=%d/%d", resolved.slug,
                     "Up" if resolved.winner_up else "Down", scored,
                     len(STRATEGIES))


def _handle_event(event: dict[str, object], now: float) -> None:
    event_type = str(event.get("event_type") or "?")
    S.events[event_type] += 1
    S.feed_health.observe(event, now)
    event_tokens = market_event_tokens(event)
    if event_tokens and not event_tokens & S.tokens:
        S.events["retired_market_event"] += 1
        return
    event_at = event_time_s(event)
    if event_type in ("book", "price_change"):
        if event_at is None:
            missing_key = f"{event_type}_missing_timestamp"
            S.events[missing_key] += 1
            if S.events[missing_key] in (1, 10, 100, 1_000, 10_000):
                log.warning(
                    "market event missing timestamp type=%s count=%d keys=%s",
                    event_type, S.events[missing_key], sorted(event),
                )
        elif stale_market_event(event, now, MAX_MARKET_EVENT_LAG_S):
            S.events["stale_market_event"] += 1
    elif event_type == "last_trade_price":
        if event_at is None:
            S.events["last_trade_missing_timestamp"] += 1
        elif now - event_at > MAX_MARKET_EVENT_LAG_S:
            S.events["delayed_trade_event"] += 1
    _record_cohort(S.engine.on_event(event, now))


def handle_event(event: dict[str, object]) -> None:
    """Compatibility handler for callers that do not provide a processing clock."""
    _handle_event(event, time.time())


def handle_event_at(
    event: dict[str, object], wall_ns: int, monotonic_ns: int,
) -> None:
    """Handle an event on the exact processing clock persisted by capture."""
    del monotonic_ns
    _handle_event(event, wall_ns / 1e9)


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
            S.engine.reset_feed()
            async with websockets.connect(
                MKT_WS, ping_interval=None, open_timeout=12, close_timeout=0.1,
                max_queue=MARKET_WS_MAX_QUEUE,
            ) as ws:
                connected_at = time.monotonic()
                await ws.send(json.dumps({
                    "assets_ids": sorted(tokens), "type": "market",
                }))
                log.info("market ws subscribed %d tokens", len(tokens))
                if S.capture is not None:
                    S.capture.connection(True)
                S.feed_pump = FeedPump(
                    handle_event, stats=S.feed_pump_stats,
                    frame_sink=(None if S.capture is None else S.capture.frame_sink),
                    processed_sink=(
                        None if S.capture is None else S.capture.processed_event
                    ),
                    timestamped_handler=handle_event_at,
                    liveness_sink=(
                        None if S.capture is None else S.capture.transport_liveness
                    ),
                )
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
            if isinstance(exc, CaptureWriteError):
                raise
            reason = f"{exc.__class__.__name__}: {exc}"
            if connected_at is not None:
                S.feed_health.reconnect()
                now = time.time()
                S.engine.disconnect(now)
                if S.capture is not None:
                    S.capture.connection(False, reason=reason, observed_at=now)
            elif S.capture is not None:
                S.capture.connection_failure(reason=reason)
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
        wall_ns = time.time_ns()
        monotonic_ns = time.monotonic_ns()
        if S.capture is not None:
            S.capture.quote_tick(wall_ns, monotonic_ns)
        now = wall_ns / 1e9
        _record_cohort(S.engine.tick(now))
        await asyncio.sleep(DECISION_CADENCE_S)


async def intent_task() -> None:
    """Publish resting quotes for live/executor.py; inert unless enabled."""
    if not S.live_gate.active:
        return
    while True:
        await asyncio.sleep(1.0)
        try:
            S.live_gate.emit(S.engine.live_quotes(), time.time())
        except Exception as exc:                     # never kill the cohort
            log.warning("intent emit failed: %s", exc)


async def heartbeat_task() -> None:
    while True:
        await asyncio.sleep(20)
        engine = S.engine.runtime_snapshot()
        feed_queue = S.feed_pump_stats.snapshot(reset_interval=True)
        log.info("hb | events=%s fills=%s active_orders=%d pending_resolution=%d "
                 "feed_paused=%s feed=%s feed_queue=%s loop=%s reference=%s gate=%s "
                 "capture=%s errors=%s",
                 dict(S.events), engine["fills"], engine["orders"], len(S.pending),
                 engine["stale_assets"],
                 S.feed_health.snapshot(reset_interval=True), feed_queue,
                 S.loop_health.snapshot(reset_interval=True),
                 S.reference_feed.snapshot(), S.live_gate.snapshot(),
                 None if S.capture is None else S.capture.snapshot(),
                 dict(S.resolution_errors))


async def report_task() -> None:
    delay = 120.0
    interval = float(os.environ.get("PAPER_SUMMARY_MINS", "15")) * 60
    while True:
        await asyncio.sleep(delay)
        delay = interval
        text = await asyncio.to_thread(report.text)
        for line in text.splitlines():
            log.info(line)
        telegram = await asyncio.to_thread(report.tg_text)
        await asyncio.to_thread(S.notify.send, telegram, pre=True)


async def kill_task() -> None:
    while not os.path.exists(KILL):
        await asyncio.sleep(1)
    raise PaperKill("paper KILL present")


async def main() -> None:
    if os.path.exists(KILL):
        raise SystemExit("paper KILL present")
    names = [config.name for config in STRATEGIES]
    S.capture = PaperCapture.from_env(
        board_hash=strategy_board_hash(STRATEGIES),
        runtime={
            "action_latency_s": ACTION_LATENCY_S,
            "max_market_event_lag_s": MAX_MARKET_EVENT_LAG_S,
            "decision_cadence_s": DECISION_CADENCE_S,
            "assets": list(ASSETS),
            "preopen_target_start": PREOPEN_TARGET_START,
        },
    )
    if S.capture is not None:
        S.ledger.record_run_metadata({
            "capture_label": S.capture.label,
            "board_hash": S.capture.board_hash,
            "model_hash": str(S.capture.model_identity["sha256"]),
        })
    log.info("focused pair paper starting | strategies=%s | queue-ahead fills | "
             "action-latency=%dms | max-market-lag=%dms | decision cadence=%dms | "
             "official Gamma outcomes | assets=%s | preopen-target=%s",
             names, round(ACTION_LATENCY_S * 1000),
             round(MAX_MARKET_EVENT_LAG_S * 1000),
             round(DECISION_CADENCE_S * 1000), list(ASSETS),
             PREOPEN_TARGET_START)
    tasks = [
        asyncio.create_task(coroutine)
        for coroutine in (
            window_task(), settlement_task(), market_task(),
            S.reference_feed.run(_record_reference), quote_task(),
            S.loop_health.run(), heartbeat_task(), report_task(), kill_task(),
            intent_task(),
            asyncio.to_thread(
                S.notify.send,
                f"focused pair paper started ({len(names)} strategies, "
                "queue-aware, no orders)",
            ),
        )
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        close_errors: list[Exception] = []
        try:
            await asyncio.to_thread(S.ledger.close)
        except Exception as exc:
            close_errors.append(exc)
        if S.capture is not None:
            try:
                await asyncio.to_thread(S.capture.close)
            except Exception as exc:
                close_errors.append(exc)
        if len(close_errors) == 1:
            raise close_errors[0]
        if close_errors:
            raise ExceptionGroup("paper output finalization failed", close_errors)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except PaperKill:
        log.info("stopped by paper KILL")
    except KeyboardInterrupt:
        log.info("stopped")
