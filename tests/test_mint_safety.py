from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from live import lockbot
from live.chain import PreflightError, merge, split
from live.feed_health import FeedHealth, event_time_s
from live.market_book import BestAskCache
from live.mint_quotes import (
    guarded_pair_prices,
    plan_pair_quotes,
    should_reprice,
    target_pair_prices,
)


class MintSafetyTests(unittest.TestCase):
    def test_feed_health_measures_server_lag_and_reconnects(self) -> None:
        health = FeedHealth(sample_size=3)
        health.observe({"timestamp": "1787631300.000"}, 1787631300.025)
        health.observe({"timestamp": "1787631300040"}, 1787631300.050)
        health.reconnect()
        self.assertEqual(
            health.snapshot(),
            {"p50_ms": 10, "p90_ms": 10, "max_ms": 25, "reconnects": 1},
        )
        self.assertEqual(event_time_s({"timestamp": "1787631300040"}), 1787631300.04)
        self.assertIsNone(event_time_s({}))

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
                     "asks": [{"price": "0.70"}]}, 1.0)
        books.apply({"event_type": "price_change", "price_changes": [
            {"asset_id": "up", "best_ask": "0.63"},
        ]}, 2.0)
        book = books.get("up")
        self.assertIsNotNone(book)
        assert book is not None
        self.assertEqual(book.price, 0.63)
        self.assertEqual(book.received_at, 2.0)
        books.clear()
        self.assertIsNone(books.get("up"))

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
