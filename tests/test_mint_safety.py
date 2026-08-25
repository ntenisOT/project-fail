from __future__ import annotations

import unittest

from live.chain import PreflightError, merge, split
from live.market_book import BestAskCache
from live.mint_quotes import plan_pair_quotes, should_reprice


class MintSafetyTests(unittest.TestCase):
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

    def test_asymmetric_fill_stops_further_quotes(self) -> None:
        plan = plan_pair_quotes(minted=20, sold_up=5, sold_down=0,
                                price_up=0.55, price_down=0.46, sum_floor=1.005)
        self.assertEqual(plan, ())

    def test_balanced_inventory_produces_one_small_pair(self) -> None:
        plan = plan_pair_quotes(minted=20, sold_up=5, sold_down=5,
                                price_up=0.55, price_down=0.46, sum_floor=1.005)
        self.assertEqual([(q.side_up, q.size) for q in plan], [(True, 5.0), (False, 5.0)])
        low_leg = plan_pair_quotes(minted=20, sold_up=0, sold_down=0,
                                   price_up=0.19, price_down=0.82, sum_floor=1.005)
        self.assertEqual([q.size for q in low_leg], [5.0, 5.0])

    def test_repricing_preserves_queue_but_urgent_underpricing_bypasses_rest(self) -> None:
        self.assertFalse(should_reprice((0.50, 0.51), (0.51, 0.50), 10.0))
        self.assertFalse(should_reprice((0.50, 0.51), (0.53, 0.48), 2.0))
        self.assertTrue(should_reprice((0.50, 0.51), (0.53, 0.48), 6.0))
        self.assertTrue(should_reprice((0.50, 0.51), (0.56, 0.45), 0.2))


if __name__ == "__main__":
    unittest.main()
