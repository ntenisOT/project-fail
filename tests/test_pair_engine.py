from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paper import report
from paper.ledger import Ledger
from paper.market_metadata import parse_active_market
from paper.order_book import OrderBook, OrderBookCache
from paper.pair_engine import PairConfig, PairWindow


def book(bid: float, bid_size: float, ask: float, ask_size: float, ts: float = 1.0) -> OrderBook:
    return OrderBook({bid: bid_size}, {ask: ask_size}, ts)


class FocusedPairTests(unittest.TestCase):
    def test_order_book_delta_updates_and_removes_levels(self) -> None:
        cache = OrderBookCache()
        cache.apply({"event_type": "book", "asset_id": "up",
                     "bids": [{"price": "0.48", "size": "10"}],
                     "asks": [{"price": "0.52", "size": "20"}]}, 1.0)
        cache.apply({"event_type": "price_change", "price_changes": [
            {"asset_id": "up", "side": "BUY", "price": "0.49", "size": "15"},
        ]}, 2.0)
        self.assertEqual(cache.get("up").best_bid, 0.49)  # type: ignore[union-attr]
        cache.apply({"event_type": "price_change", "price_changes": [
            {"asset_id": "up", "side": "BUY", "price": "0.49", "size": "0"},
        ]}, 3.0)
        self.assertEqual(cache.get("up").best_bid, 0.48)  # type: ignore[union-attr]

    def test_public_queue_depth_must_trade_before_our_fill(self) -> None:
        window = PairWindow(PairConfig("carry", "accumulate", 0.6, action_latency_s=0),
                            "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        window.on_books(1.0, book(0.48, 10, 0.52, 5), book(0.49, 10, 0.51, 5))
        self.assertIsNone(window.on_trade(1.1, True, 0.48, 8, "SELL"))
        fill = window.on_trade(1.2, True, 0.48, 5, "SELL")
        self.assertEqual(fill["size"], 3.0)  # type: ignore[index]
        self.assertEqual(window.queue_consumed, 10.0)

    def test_order_cannot_fill_before_modeled_action_latency(self) -> None:
        window = PairWindow(PairConfig("carry", "accumulate", 0.01, 0.06),
                            "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        up, down = book(0.48, 0, 0.52, 5), book(0.49, 0, 0.51, 5)
        window.on_books(1.0, up, down)
        self.assertFalse(window.orders)
        self.assertIsNone(window.on_trade(1.03, True, 0.48, 5, "SELL"))
        window.on_books(1.06, up, down)
        self.assertIn((True, "buy"), window.orders)

    def test_late_first_books_make_the_window_unscored(self) -> None:
        window = PairWindow(PairConfig("carry", "accumulate", 0.01, 0),
                            "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        window.on_books(10.01, book(0.48, 0, 0.52, 5), book(0.49, 0, 0.51, 5))
        self.assertFalse(window.full_window)
        self.assertFalse(window.orders)

    def test_delayed_requote_cannot_oversell_after_an_inflight_fill(self) -> None:
        config = PairConfig("mint", "mint", 0.01, 0.06, mint_sets=5)
        window = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        first = book(0.18, 0, 0.20, 0), book(0.79, 0, 0.81, 0)
        moved = book(0.28, 0, 0.30, 0), book(0.69, 0, 0.71, 0)
        window.on_books(1.0, *first)
        window.on_books(1.06, *first)
        window.on_books(1.07, *moved)
        window.on_trade(1.08, True, 0.20, 5, "BUY")
        window.on_books(1.13, *moved)
        self.assertEqual(window.inventory[True], 0)
        self.assertNotIn((True, "sell"), window.orders)
        self.assertEqual(window.orders[(False, "sell")].price, 0.81)

    def test_discounted_pair_has_outcome_independent_profit(self) -> None:
        for outcome in (0, 1):
            window = PairWindow(PairConfig("carry", "accumulate", 0.6, action_latency_s=0),
                                "btc", "btc-updown-5m-0", 0, "up", "down", 0)
            window.on_books(1.0, book(0.48, 0, 0.52, 5), book(0.49, 0, 0.51, 5))
            window.on_trade(1.1, True, 0.48, 5, "SELL")
            window.on_trade(1.2, False, 0.49, 5, "SELL")
            settled, _ = window.settle(300.0, outcome)
            self.assertAlmostEqual(float(settled["pnl"]), 0.15)

    def test_open_buy_leg_caps_the_actual_completion_price(self) -> None:
        window = PairWindow(PairConfig("carry", "accumulate", 0.6, action_latency_s=0),
                            "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        window.on_books(1.0, book(0.80, 0, 0.82, 5), book(0.19, 0, 0.21, 5))
        window.on_trade(1.1, True, 0.80, 5, "SELL")
        window.on_books(1.7, book(0.70, 0, 0.72, 5), book(0.29, 0, 0.31, 5))
        self.assertEqual(window.orders[(False, "buy")].price, 0.19)
        window.on_trade(1.8, False, 0.19, 5, "SELL")
        _, metrics = window.settle(300, 1)
        self.assertAlmostEqual(metrics["buy_pair_cost"] / metrics["buy_pair_shares"], 0.99)

    def test_open_sell_leg_floors_the_actual_completion_price(self) -> None:
        config = PairConfig("mint", "mint", 0.6, action_latency_s=0,
                            sell_sum_floor=1.005)
        window = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        window.on_books(1.0, book(0.18, 0, 0.20, 0), book(0.79, 0, 0.81, 0))
        window.on_trade(1.1, True, 0.20, 5, "BUY")
        window.on_books(1.7, book(0.78, 0, 0.80, 0), book(0.19, 0, 0.21, 0))
        self.assertEqual(window.orders[(False, "sell")].price, 0.81)
        window.on_trade(1.8, False, 0.81, 5, "BUY")
        _, metrics = window.settle(300, 1)
        self.assertAlmostEqual(
            metrics["sell_pair_proceeds"] / metrics["sell_pair_shares"], 1.01
        )

    def test_churn_buys_below_one_and_sells_above_one(self) -> None:
        window = PairWindow(PairConfig("churn", "churn", 0.6, action_latency_s=0),
                            "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        up, down = book(0.48, 0, 0.52, 0), book(0.49, 0, 0.51, 0)
        window.on_books(1.0, up, down)
        window.on_trade(1.1, True, 0.48, 5, "SELL")
        window.on_trade(1.2, False, 0.49, 5, "SELL")
        window.on_books(1.7, up, down)
        window.on_trade(1.8, True, 0.52, 5, "BUY")
        window.on_trade(1.9, False, 0.51, 5, "BUY")
        settled, metrics = window.settle(300.0, 0)
        self.assertAlmostEqual(float(settled["pnl"]), 0.30)
        self.assertAlmostEqual(metrics["buy_pair_cost"] / metrics["buy_pair_shares"], 0.97)
        self.assertAlmostEqual(metrics["sell_pair_proceeds"] / metrics["sell_pair_shares"], 1.03)

    def test_active_market_parser_refuses_a_different_returned_slug(self) -> None:
        payload = [{"markets": [{
            "slug": "eth-updown-5m-0", "conditionId": "0x" + "1" * 64,
            "outcomes": '["Up","Down"]', "clobTokenIds": '["11","22"]',
        }]}]
        self.assertIsNone(parse_active_market("btc", 0, payload))

    def test_report_reads_settlement_and_queue_metrics(self) -> None:
        with TemporaryDirectory() as temp:
            path = str(Path(temp) / "paper.db")
            ledger = Ledger(path)
            window = PairWindow(PairConfig("carry", "accumulate", 0.6, action_latency_s=0),
                                "btc", "btc-updown-5m-0", 0, "up", "down", 0)
            window.on_books(1.0, book(0.48, 0, 0.52, 5), book(0.49, 0, 0.51, 5))
            for side, price in ((True, 0.48), (False, 0.49)):
                fill = window.on_trade(1.1, side, price, 5, "SELL")
                assert fill is not None
                ledger.record_fill(1.1, "carry", "btc", window.slug, fill)
            outcomes = [row[0] for row in ledger.db.execute(
                "SELECT outcome_up FROM fills ORDER BY rowid"
            )]
            self.assertEqual(outcomes, [1, 0])
            settled, metrics = window.settle(300.0, 1)
            ledger.record_settlement(300.0, "carry", "btc", window.slug, settled)
            ledger.record_metrics(300.0, "carry", "btc", window.slug, metrics)
            ledger.record_fill(301, "carry", "btc", "btc-updown-5m-300", {
                "action": "buy", "price": 0.5, "size": 100,
                "signed_cash": -50, "outcome_up": 1,
            })
            snapshot = report.snapshot_one(ledger.db, "carry")
            self.assertAlmostEqual(snapshot.volume, 4.85)
            output = report.text(path)
            self.assertIn("carry", output)
            self.assertIn("0.970", output)
            ledger.close()


if __name__ == "__main__":
    unittest.main()
