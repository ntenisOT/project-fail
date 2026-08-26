from __future__ import annotations

import asyncio
import collections
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from live import lockbot, mintbot
from live.chain import PreflightError, merge, split
from live.feed_health import (
    FeedHealth,
    event_time_s,
    market_event_tokens,
    stale_market_event,
)
from live.feed_pump import FeedPumpStats
from live.market_book import BestAskCache, fresh_ask_pair
from live.mint_quotes import (
    Quote,
    guarded_pair_prices,
    plan_pair_quotes,
    should_reprice,
    target_pair_prices,
)
from live.window_clock import boundary_aligned_delay


class MintSafetyTests(unittest.TestCase):
    def test_rotation_poll_aligns_to_window_boundary(self) -> None:
        self.assertAlmostEqual(boundary_aligned_delay(299.8), 0.2)
        self.assertEqual(boundary_aligned_delay(250.0), 0.5)
        self.assertEqual(boundary_aligned_delay(299.999), 0.01)

    def test_feed_health_measures_server_lag_and_reconnects(self) -> None:
        health = FeedHealth(sample_size=3)
        health.observe({"timestamp": "1787631300.000"}, 1787631300.025)
        health.observe({"timestamp": "1787631300040"}, 1787631300.050)
        health.reconnect()
        self.assertEqual(
            health.snapshot(),
            {"p50_ms": 10, "p90_ms": 10, "max_ms": 25,
             "interval_max_ms": 25, "lifetime_max_ms": 25,
             "missing_timestamps": 0, "out_of_range_timestamps": 0,
             "future_timestamps": 0, "max_future_ms": 0,
             "reconnects": 1},
        )
        health.observe({}, 1787631300.060)
        health.observe({"timestamp": "1787631230"}, 1787631300.060)
        self.assertEqual(health.snapshot(reset_interval=True)["interval_max_ms"], 25)
        self.assertEqual(health.snapshot()["interval_max_ms"], 0)
        self.assertEqual(health.snapshot()["missing_timestamps"], 1)
        self.assertEqual(health.snapshot()["out_of_range_timestamps"], 1)
        self.assertEqual(event_time_s({"timestamp": "1787631300040"}), 1787631300.04)
        self.assertIsNone(event_time_s({}))

    def test_stale_market_delta_fails_closed_but_delayed_trade_does_not(self) -> None:
        delta = {"event_type": "price_change", "timestamp": "1000"}
        trade = {"event_type": "last_trade_price", "timestamp": "1000"}
        self.assertTrue(stale_market_event(delta, 1000.5, 0.4))
        self.assertTrue(stale_market_event({"event_type": "book"}, 1000.0, 0.4))
        self.assertFalse(stale_market_event(delta, 1000.3, 0.4))
        self.assertTrue(stale_market_event(delta, 999.9, 0.4))
        self.assertFalse(stale_market_event(trade, 1001.0, 0.4))
        self.assertIsNone(event_time_s({"timestamp": "nan"}))
        self.assertTrue(stale_market_event(
            {"event_type": "book", "timestamp": "nan"}, 1000.0, 0.4,
        ))
        self.assertEqual(
            market_event_tokens({
                "event_type": "price_change", "price_changes": [
                    {"asset_id": "up"}, {"asset_id": "down"}, {},
                ],
            }),
            {"up", "down"},
        )

    def test_retired_lockbot_place_mode_is_fail_closed(self) -> None:
        original = lockbot.MODE
        lockbot.MODE = "place"
        try:
            with self.assertRaisesRegex(SystemExit, "PLACE DISABLED"):
                lockbot.Lockbot()
        finally:
            lockbot.MODE = original
        env = dict(os.environ)
        env["LOCKBOT_MODE"] = "place"
        result = subprocess.run(
            [sys.executable, "-m", "live.lockbot"],
            cwd=Path(__file__).resolve().parents[1], env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PLACE DISABLED", result.stderr + result.stdout)

    def test_unverified_v2_direct_mint_path_is_fail_closed(self) -> None:
        for operation in (split, merge):
            with self.assertRaisesRegex(PreflightError, "CLOB V2"):
                operation("unused", "0x" + "0" * 64, 1.0)

    def test_price_change_replaces_stale_snapshot(self) -> None:
        books = BestAskCache()
        books.apply({"event_type": "book", "asset_id": "up",
                     "bids": [], "asks": [{"price": "0.70"}]}, 1.0)
        books.apply({"event_type": "book", "asset_id": "down",
                     "bids": [], "asks": [{"price": "0.40"}]}, 1.0)
        books.apply({"event_type": "price_change", "price_changes": [
            {"asset_id": "up", "best_ask": "0.63"},
        ]}, 2.0, source_at=2.0)
        book = books.get("up")
        self.assertIsNotNone(book)
        assert book is not None
        self.assertEqual(book.price, 0.63)
        self.assertEqual(book.received_at, 2.0)
        self.assertEqual(book.source_at, 2.0)
        books.apply({"event_type": "price_change", "price_changes": [
            {"asset_id": "up", "best_ask": "0.20"},
        ]}, 2.1, source_at=1.9)
        self.assertEqual(books.get("up"), book)
        self.assertIsNone(fresh_ask_pair(books, "up", "down", 2.1, 0.5))
        books.apply({"event_type": "price_change", "price_changes": [
            {"asset_id": "down", "best_ask": "0.39"},
        ]}, 2.0)
        self.assertEqual(fresh_ask_pair(books, "up", "down", 2.1, 0.5),
                         (0.63, 0.39))
        books.clear()
        self.assertIsNone(books.get("up"))

    def test_best_ask_freshness_bounds_source_age_and_nonfinite_time(self) -> None:
        books = BestAskCache()
        books.apply({"event_type": "book", "asset_id": "up",
                     "bids": [], "asks": [{"price": "0.60"}]},
                    10.0, source_at=9.7)
        books.apply({"event_type": "book", "asset_id": "down",
                     "bids": [], "asks": [{"price": "0.40"}]},
                    10.0, source_at=9.7)
        self.assertIsNone(fresh_ask_pair(books, "up", "down", 10.11, 0.4))
        with self.assertRaisesRegex(ValueError, "finite and positive"):
            books.apply({"event_type": "book", "asset_id": "up",
                         "bids": [], "asks": []}, 10.2, source_at=float("nan"))

    def test_mint_malformed_mixed_delta_clears_every_book(self) -> None:
        bot = mintbot.Mintbot.__new__(mintbot.Mintbot)
        bot.books = BestAskCache()
        now = time.time()
        bot.feed_health = FeedHealth()
        bot.feed_counts = collections.Counter()
        bot.quote_counts = collections.Counter()
        bot.quote_wakeup = asyncio.Event()
        bot.last_feed_log = time.monotonic()
        bot.feed_pump_stats = FeedPumpStats()
        for timestamp in (now, now - 10):
            for token in ("up", "down"):
                bot.books.apply({
                    "event_type": "book", "asset_id": token,
                    "bids": [], "asks": [{"price": "0.50"}],
                }, now, source_at=now)
            bot.on_market_event({
                "event_type": "price_change", "timestamp": timestamp,
                "price_changes": [
                    {"asset_id": "up", "best_ask": "0.49"},
                    {"best_ask": "0.51"},
                ],
            })
            self.assertIsNone(bot.books.get("up"))
            self.assertIsNone(bot.books.get("down"))

    def test_feed_change_during_verified_cancel_aborts_stale_plan(self) -> None:
        async def scenario() -> None:
            started, release = threading.Event(), threading.Event()

            class FakeClob:
                def __init__(self) -> None:
                    self.placements: list[object] = []

                def cancel_many_verified(self, _order_ids) -> None:
                    started.set()
                    assert release.wait(2)

                def place_many(self, orders, _post_only):
                    self.placements.append(orders)
                    return ["new-up", "new-down"]

                def cancel_all_verified(self) -> None:
                    return None

            bot = mintbot.Mintbot.__new__(mintbot.Mintbot)
            bot.books = BestAskCache()
            bot.quote_counts = collections.Counter()
            bot.clob = FakeClob()
            now = time.time()
            for token, ask in (("up", "0.60"), ("down", "0.40")):
                bot.books.apply({
                    "event_type": "book", "asset_id": token,
                    "bids": [], "asks": [{"price": ask}],
                }, now, source_at=now)
            generation = bot.books.revision("up", "down")
            state = {
                "up": "up", "dn": "down", "asset": "btc", "closing": False,
                "asks": {True: (0.50, "old-up"), False: (0.50, "old-down")},
                "pair_placed_at": now - 20, "quote_lock": asyncio.Lock(),
            }
            plan = (Quote(True, 0.65, 5), Quote(False, 0.40, 5))
            old_mode = mintbot.MODE
            mintbot.MODE = "place"
            try:
                task = asyncio.create_task(bot.requote_pair(state, plan, generation))
                assert await asyncio.to_thread(started.wait, 1)
                bot.books.drop("up")
                release.set()
                await task
            finally:
                mintbot.MODE = old_mode
                release.set()
            self.assertEqual(bot.clob.placements, [])
            self.assertEqual(bot.quote_counts["stale_plan_abort"], 1)

        asyncio.run(scenario())

    def test_asymmetric_fill_stops_further_quotes(self) -> None:
        plan = plan_pair_quotes(minted=20, sold_up=5, sold_down=0,
                                price_up=0.55, price_down=0.46, sum_floor=1.005)
        self.assertEqual(plan, ())

    def test_balanced_inventory_produces_one_small_pair(self) -> None:
        self.assertEqual(
            target_pair_prices(0.40, 0.60, spread=0.02, sum_floor=1.005),
            (0.42, 0.62),
        )
        plan = plan_pair_quotes(minted=20, sold_up=5, sold_down=5,
                                price_up=0.55, price_down=0.46, sum_floor=1.005)
        self.assertEqual([(q.side_up, q.size) for q in plan], [(True, 5.0), (False, 5.0)])
        low_leg = plan_pair_quotes(minted=20, sold_up=0, sold_down=0,
                                   price_up=0.19, price_down=0.82, sum_floor=1.005)
        self.assertEqual([q.size for q in low_leg], [5.0, 5.0])

    def test_endpoint_books_pause_instead_of_crashing_the_quote_loop(self) -> None:
        self.assertIsNone(
            guarded_pair_prices(1.0, 0.40, spread=0.02, sum_floor=1.005)
        )
        self.assertEqual(
            guarded_pair_prices(0.40, 0.60, spread=0.02, sum_floor=1.005),
            (0.42, 0.62),
        )

    def test_repricing_preserves_queue_but_urgent_underpricing_bypasses_rest(self) -> None:
        self.assertFalse(should_reprice((0.50, 0.51), (0.51, 0.50), 10.0))
        self.assertFalse(should_reprice((0.50, 0.51), (0.53, 0.48), 2.0))
        self.assertTrue(should_reprice((0.50, 0.51), (0.55, 0.46), 16.0))
        self.assertTrue(should_reprice((0.50, 0.51), (0.60, 0.41), 0.2))


if __name__ == "__main__":
    unittest.main()
