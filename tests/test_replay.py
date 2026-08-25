from __future__ import annotations

import json
import time

from paper.capture import PaperCapture
from paper.cohort_engine import CohortEngine, CohortRecord
from paper.market_metadata import ActiveMarket
from paper.pair_types import PairConfig
from paper.replay import replay_dataset
from paper.strategy_board import strategy_board_hash
from tools.market_windows import ResolvedWindow


def test_capture_replay_runs_same_queue_engine_deterministically(tmp_path) -> None:
    config = PairConfig(
        "pair", "accumulate", 0.001, action_latency_s=0,
        buy_sum_ceiling=0.99, max_inventory=5,
    )
    runtime = {
        "action_latency_s": 0, "max_market_event_lag_s": 0.4,
        "decision_cadence_s": 0.001,
    }
    capture = PaperCapture(
        tmp_path, "replay", board_hash=strategy_board_hash((config,)),
        runtime=runtime, limit_bytes=10_000, chunk_bytes=10_000,
    )
    start = int(time.time())
    market = ActiveMarket(
        "btc", f"btc-updown-5m-{start}", start, "condition", "up", "down", 5,
    )
    live = CohortEngine((config,), max_event_lag_s=0.4)
    live_records: list[CohortRecord] = []
    live.open_market(market, float(start))
    capture.market_open(market, float(start))
    capture.connection_failure(reason="open timeout")
    capture.connection(True)
    snapshots = [
        {"event_type": "book", "asset_id": token, "timestamp": start + 1,
         "bids": [{"price": bid, "size": 1}],
         "asks": [{"price": ask, "size": 5}]}
        for token, bid, ask in (("up", 0.48, 0.52), ("down", 0.49, 0.51))
    ]
    first_mono = time.monotonic_ns() + 1_000_000
    book_frame = capture.frame_sink(
        (start + 1) * 1_000_000_000, first_mono, json.dumps(snapshots),
    )
    capture.processed_event(
        (start + 1) * 1_000_000_000, first_mono + 100_000, book_frame, 0,
    )
    live_records.extend(live.on_event(snapshots[0], start + 1))
    capture.processed_event(
        (start + 1) * 1_000_000_000, first_mono + 200_000, book_frame, 1,
    )
    live_records.extend(live.on_event(snapshots[1], start + 1))
    capture.quote_tick(
        int((start + 1.001) * 1_000_000_000), first_mono + 1_000_000,
    )
    live_records.extend(live.tick(start + 1.001))
    trades = [
        {"event_type": "last_trade_price", "asset_id": token,
         "timestamp": ts, "side": "SELL", "price": price, "size": 6}
        for token, ts, price in (
            ("up", start + 1.003, 0.48), ("down", start + 1.003, 0.49),
        )
    ]
    trade_frame = capture.frame_sink(
        int((start + 1.003) * 1_000_000_000), first_mono + 3_000_000,
        json.dumps(trades),
    )
    capture.processed_event(
        int((start + 1.003) * 1_000_000_000), first_mono + 3_100_000,
        trade_frame, 0,
    )
    live_records.extend(live.on_event(trades[0], start + 1.003))
    capture.processed_event(
        int((start + 1.003) * 1_000_000_000), first_mono + 3_200_000,
        trade_frame, 1,
    )
    live_records.extend(live.on_event(trades[1], start + 1.003))
    time.sleep(0.006)
    live.finish_window(market.asset, start + 300)
    capture.market_finish(market.asset, market.slug, market.start, start + 300)
    capture.resolution(
        ResolvedWindow(market.slug, market.asset, 0, market.condition_id,
                       market.up_token, market.down_token, 1), start + 315,
    )
    live_records.extend(live.settle("btc", 1, start + 315, slug=market.slug))
    capture.close()

    first = replay_dataset(tmp_path / "replay.dataset.json", (config,))
    second = replay_dataset(tmp_path / "replay.dataset.json", (config,))

    assert first == second
    assert first.records == tuple(live_records)
    assert first.frames == 2 and first.parse_errors == 0
    assert (
        first.opened_markets, first.finished_markets, first.resolved_markets,
        first.open_at_end, first.finished_unresolved,
        first.settled_strategy_windows, first.invalid_strategy_windows,
    ) == (1, 1, 1, 0, 0, 1, 0)
    assert [record.__class__.__name__ for record in first.records] == [
        "FillRecord", "FillRecord", "SettlementRecord",
    ]


def test_replay_uses_captured_tick_phase_instead_of_synthetic_cadence(tmp_path) -> None:
    config = PairConfig(
        "phase", "accumulate", 0.001, action_latency_s=0,
        buy_sum_ceiling=0.99, max_inventory=5,
    )
    capture = PaperCapture(
        tmp_path, "phase", board_hash=strategy_board_hash((config,)),
        runtime={
            "action_latency_s": 0, "max_market_event_lag_s": 0.4,
            "decision_cadence_s": 0.000001,
        },
        limit_bytes=20_000, chunk_bytes=20_000,
    )
    start = int(time.time())
    market = ActiveMarket(
        "btc", f"btc-updown-5m-{start}", start, "condition", "up", "down", 5,
    )
    capture.market_open(market, float(start))
    capture.connection(True)
    base_mono = time.monotonic_ns()
    books = [
        {"event_type": "book", "asset_id": token, "timestamp": start + 1,
         "bids": [{"price": bid, "size": 1}],
         "asks": [{"price": ask, "size": 5}]}
        for token, bid, ask in (("up", 0.48, 0.52), ("down", 0.49, 0.51))
    ]
    book_frame = capture.frame_sink(
        int((start + 1) * 1e9), base_mono, json.dumps(books),
    )
    capture.processed_event(int((start + 1) * 1e9), base_mono + 100, book_frame, 0)
    capture.processed_event(int((start + 1) * 1e9), base_mono + 200, book_frame, 1)

    early_trade = {
        "event_type": "last_trade_price", "asset_id": "up",
        "timestamp": start + 1.001, "side": "SELL", "price": 0.48, "size": 6,
    }
    early_frame = capture.frame_sink(
        int((start + 1.001) * 1e9), base_mono + 300, json.dumps(early_trade),
    )
    capture.processed_event(
        int((start + 1.001) * 1e9), base_mono + 400, early_frame, 0,
    )
    capture.quote_tick(int((start + 1.002) * 1e9), base_mono + 500)

    late_trade = dict(early_trade, timestamp=start + 1.003)
    late_frame = capture.frame_sink(
        int((start + 1.003) * 1e9), base_mono + 600, json.dumps(late_trade),
    )
    capture.processed_event(
        int((start + 1.003) * 1e9), base_mono + 700, late_frame, 0,
    )
    time.sleep(0.001)
    capture.market_finish(market.asset, market.slug, market.start, start + 300)
    capture.resolution(
        ResolvedWindow(market.slug, market.asset, market.start, market.condition_id,
                       market.up_token, market.down_token, 1),
        start + 315,
    )
    capture.close()

    result = replay_dataset(tmp_path / "phase.dataset.json", (config,))

    fills = [row for row in result.records if row.__class__.__name__ == "FillRecord"]
    assert result.decision_ticks == 1
    assert result.market_events == 4
    assert len(fills) == 1
    assert fills[0].ts == late_trade["timestamp"]
