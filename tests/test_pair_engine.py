from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paper import report
from paper.ledger import Ledger
from paper.market_metadata import parse_active_market
from paper.order_book import OrderBook, OrderBookCache
from paper.pair_engine import PairConfig, PairWindow
from paper.settlement import settle_valid
from paper.taker import crypto_fee, crypto_maker_rebate, sweep


def book(bid: float, bid_size: float, ask: float, ask_size: float, ts: float = 1.0) -> OrderBook:
    return OrderBook({bid: bid_size}, {ask: ask_size}, ts)


class FocusedPairTests(unittest.TestCase):
    def test_taker_sweep_requires_full_depth_and_minimum_notional(self) -> None:
        depth = OrderBook({0.42: 2, 0.41: 4}, {0.58: 2, 0.59: 4}, 1)
        legs = sweep(depth, "sell", 5)
        self.assertEqual([(leg.price, leg.shares) for leg in legs], [(0.42, 2), (0.41, 3)])
        self.assertEqual([leg.fee for leg in legs], [crypto_fee(0.42, 2), crypto_fee(0.41, 3)])
        self.assertFalse(sweep(OrderBook({0.42: 4}, {}, 1), "sell", 5))
        self.assertFalse(sweep(OrderBook({0.10: 5}, {}, 1), "sell", 5))

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

    def test_resting_bids_count_as_peak_committed_capital(self) -> None:
        window = PairWindow(PairConfig("capital", "accumulate", 0.6, action_latency_s=0),
                            "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        window.on_books(1.0, book(0.48, 10, 0.52, 5), book(0.49, 10, 0.51, 5))

        settled, _ = window.settle(300.0, 1)

        self.assertAlmostEqual(float(settled["capital"]), 4.85)

    def test_order_cannot_fill_before_modeled_action_latency(self) -> None:
        window = PairWindow(PairConfig("carry", "accumulate", 0.01, 0.06),
                            "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        up, down = book(0.48, 0, 0.52, 5), book(0.49, 0, 0.51, 5)
        window.on_books(1.0, up, down)
        self.assertFalse(window.orders)
        self.assertIsNone(window.on_trade(1.03, True, 0.48, 5, "SELL"))
        window.on_books(1.06, up, down)
        self.assertIn((True, "buy"), window.orders)
        self.assertIsNone(window.on_trade(1.05, True, 0.48, 5, "SELL"))
        self.assertEqual(window.pre_activation_trades, 1)

    def test_late_first_books_make_the_window_unscored(self) -> None:
        window = PairWindow(PairConfig("carry", "accumulate", 0.01, 0),
                            "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        window.on_books(10.01, book(0.48, 0, 0.52, 5), book(0.49, 0, 0.51, 5))
        self.assertFalse(window.full_window)
        self.assertEqual(window.invalid_reason, "late_first_books")
        self.assertFalse(window.orders)

    def test_feed_gap_invalidates_window_and_closes_orders(self) -> None:
        window = PairWindow(PairConfig("carry", "accumulate", 0.01, 0),
                            "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        window.on_books(1.0, book(0.48, 0, 0.52, 5), book(0.49, 0, 0.51, 5))
        self.assertTrue(window.orders)
        window.invalidate(2.0, "ws_reconnect")
        self.assertFalse(window.full_window)
        self.assertEqual(window.invalid_reason, "ws_reconnect")
        self.assertFalse(window.orders)
        self.assertIsNone(window.on_trade(2.1, True, 0.48, 5, "SELL"))

    def test_feed_tail_freezes_decisions_but_keeps_exchange_exposure_scored(self) -> None:
        window = PairWindow(PairConfig("lagged", "accumulate", 0.01, 0),
                            "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        window.on_books(1.0, book(0.48, 0, 0.52, 5), book(0.49, 0, 0.51, 5))
        window.observe_stale_market_event(812)

        self.assertTrue(window.full_window)
        self.assertTrue(window.orders)
        fill = window.on_trade(1.5, True, 0.48, 5, "SELL", received_at=2.0)
        self.assertIsNotNone(fill)
        _, metrics = window.settle(300, 1)
        self.assertEqual(metrics["exposed_stale_market_events"], 1)
        self.assertEqual(metrics["max_exposed_stale_event_lag_ms"], 812)

    def test_settlement_scores_valid_strategies_independently(self) -> None:
        valid = PairWindow(PairConfig("valid", "accumulate", 0.01, 0),
                           "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        invalid = PairWindow(PairConfig("invalid", "accumulate", 0.01, 0),
                             "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        invalid.invalidate(2.0, "stale_market_event", 812)

        scored, skipped = settle_valid({"valid": valid, "invalid": invalid}, 300, 1)

        self.assertEqual([row.strategy for row in scored], ["valid"])
        self.assertEqual([(row.strategy, row.reason) for row in skipped], [
            ("invalid", "stale_market_event"),
        ])
        self.assertEqual(skipped[0].event_lag_ms, 812)

    def test_entry_cutoff_cancels_balanced_quotes_but_completes_an_open_leg(self) -> None:
        config = PairConfig("cutoff", "accumulate", 0.01, 0, new_pair_cutoff_s=240)
        up, down = book(0.48, 0, 0.52, 5), book(0.49, 0, 0.51, 5)
        balanced = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        balanced.on_books(1, up, down)
        balanced.on_books(240, up, down)
        self.assertFalse(balanced.orders)

        open_leg = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        open_leg.on_books(1, up, down)
        open_leg.on_trade(2, True, 0.48, 5, "SELL")
        open_leg.on_books(240, up, down)
        self.assertIn((False, "buy"), open_leg.orders)
        self.assertNotIn((True, "buy"), open_leg.orders)

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
            window.observe_stale_market_event(812)
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

    def test_basket_cap_reinvests_prior_pair_surplus(self) -> None:
        config = PairConfig("basket", "accumulate", 0.01, action_latency_s=0,
                            buy_sum_ceiling=0.98, require_both_to_start=True,
                            basket_average_cap=True)
        window = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        first = book(0.40, 0, 0.42, 5), book(0.50, 0, 0.52, 5)
        window.on_books(1.0, *first)
        window.on_trade(1.1, True, 0.40, 5, "SELL")
        window.on_trade(1.2, False, 0.50, 5, "SELL")
        second = book(0.60, 0, 0.62, 5), book(0.44, 0, 0.46, 5)
        window.on_books(1.3, *second)
        self.assertEqual({order.price for order in window.orders.values()}, {0.60, 0.44})
        window.on_trade(1.4, True, 0.60, 5, "SELL")
        window.on_trade(1.5, False, 0.44, 5, "SELL")
        _, metrics = window.settle(300, 1)
        self.assertAlmostEqual(metrics["buy_pair_cost"] / metrics["buy_pair_shares"], 0.97)

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

    def test_mint_hedge_closes_open_pair_after_wait_with_fee(self) -> None:
        config = PairConfig(
            "hedge", "mint", 0.01, action_latency_s=0,
            mint_sets=5, sell_sum_floor=1.005, taker_hedge_after_s=5,
        )
        window = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        up, down = book(0.58, 5, 0.60, 0), book(0.43, 5, 0.44, 0)
        window.on_books(1.0, up, down)
        window.on_trade(1.1, True, 0.60, 5, "BUY")
        self.assertFalse(window.on_books(6.09, up, down))
        fills = window.on_books(6.1, up, down)
        self.assertEqual([(fill["action"], fill["outcome_up"]) for fill in fills],
                         [("taker_sell", 0)])
        self.assertEqual(window.inventory, {True: 0, False: 0})
        settled, metrics = window.settle(300, 0)
        fee = crypto_fee(0.43, 5)
        self.assertAlmostEqual(float(settled["pnl"]), 5 * (0.60 + 0.43 - 1) - fee)
        self.assertAlmostEqual(metrics["taker_fees"], fee)
        self.assertAlmostEqual(metrics["unmatched_end"], 0)

    def test_mint_hedge_refuses_a_pair_below_the_net_floor(self) -> None:
        config = PairConfig(
            "hedge", "mint", 0.01, action_latency_s=0,
            mint_sets=5, sell_sum_floor=1.005, taker_hedge_after_s=5,
        )
        window = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        up = book(0.58, 5, 0.60, 0)
        window.on_books(1.0, up, book(0.43, 5, 0.44, 0))
        window.on_trade(1.1, True, 0.60, 5, "BUY")
        down = book(0.30, 5, 0.31, 0)
        window.on_books(2.0, up, down)
        self.assertFalse(window.on_books(6.1, up, down))
        self.assertEqual(window.inventory, {True: 0, False: 5})
        self.assertEqual(window.taker_fees, 0)

    def test_mint_flatten_crosses_below_floor_after_action_delay(self) -> None:
        config = PairConfig(
            "flatten", "mint", 0.01, action_latency_s=0.065,
            mint_sets=5, sell_sum_floor=1.005, taker_hedge_after_s=0,
            taker_pair_sum_floor=0,
        )
        window = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        up = book(0.58, 5, 0.60, 0)
        window.on_books(1.0, up, book(0.43, 5, 0.44, 0))
        window.on_books(1.065, up, book(0.43, 5, 0.44, 0))
        window.on_trade(1.1, True, 0.60, 5, "BUY", received_at=1.4)
        down = book(0.30, 5, 0.31, 0)
        self.assertFalse(window.on_books(1.464, up, down))
        fills = window.on_books(1.465, up, down)
        self.assertEqual([fill["action"] for fill in fills], ["taker_sell"])
        self.assertEqual(window.inventory, {True: 0, False: 0})
        self.assertGreater(window.taker_fees, 0)

    def test_mint_cycle_anchors_and_completes_asymmetric_fill(self) -> None:
        config = PairConfig(
            "mint", "mint", 0.5, action_latency_s=0,
            new_pair_start_s=3, new_pair_cutoff_s=275,
            mint_anchor_spread=0.02,
        )
        window = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        up, down = book(0.38, 10, 0.40, 10), book(0.58, 10, 0.60, 10)
        window.on_books(1.0, up, down)
        self.assertFalse(window.orders)
        window.on_books(3.0, up, down)
        self.assertEqual(window.orders[(True, "sell")].price, 0.42)
        self.assertEqual(window.orders[(False, "sell")].price, 0.62)
        window.on_trade(3.1, True, 0.42, 5, "BUY")
        window.on_books(3.5, up, down)
        self.assertNotIn((True, "sell"), window.orders)
        self.assertEqual(window.orders[(False, "sell")].price, 0.62)
        window.on_trade(3.6, False, 0.62, 5, "BUY")
        window.on_books(4.0, up, down)
        self.assertEqual(set(window.orders), {(True, "sell"), (False, "sell")})

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

    def test_inventory_churn_sells_sets_then_replenishes_below_one(self) -> None:
        config = PairConfig(
            "inventory", "inventory", 0.01, action_latency_s=0,
            initial_sets=10, max_inventory=10,
        )
        window = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        up, down = book(0.48, 0, 0.52, 0), book(0.48, 0, 0.52, 0)
        window.on_books(1.0, up, down)
        self.assertEqual(set(window.orders), {(True, "sell"), (False, "sell")})
        window.on_trade(1.1, True, 0.52, 5, "BUY")
        window.on_trade(1.2, False, 0.52, 5, "BUY")
        window.on_books(1.3, up, down)
        self.assertEqual(set(window.orders), {(True, "buy"), (False, "buy")})
        window.on_trade(1.4, True, 0.48, 5, "SELL")
        window.on_trade(1.5, False, 0.48, 5, "SELL")
        settled, metrics = window.settle(300, 0)
        self.assertAlmostEqual(float(settled["pnl"]), 0.4)
        self.assertAlmostEqual(metrics["buy_pair_cost"], 4.8)
        self.assertAlmostEqual(metrics["sell_pair_proceeds"], 5.2)

    def test_inventory_pair_mm_never_starts_with_one_eligible_leg(self) -> None:
        config = PairConfig(
            "inventory_mm", "churn", 0.01, action_latency_s=0,
            initial_sets=10, max_inventory=10, require_both_to_start=True,
        )
        window = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        up, down = book(0.48, 0, 0.52, 0), book(0.48, 0, 0.52, 0)
        window.on_books(1.0, up, down)
        self.assertEqual(set(window.orders), {(True, "sell"), (False, "sell")})
        window.on_trade(1.1, True, 0.52, 5, "BUY")
        window.on_books(1.15, up, down)
        self.assertEqual(set(window.orders), {(False, "sell")})
        window.on_trade(1.2, False, 0.52, 5, "BUY")
        window.on_books(1.3, up, down)
        self.assertEqual(set(window.orders), {
            (True, "buy"), (False, "buy"),
            (True, "sell"), (False, "sell"),
        })

    def test_inside_quotes_trade_pair_margin_for_queue_priority(self) -> None:
        config = PairConfig("inside", "churn", 0.01, action_latency_s=0,
                            improve_ticks=1)
        window = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
        up, down = book(0.47, 10, 0.52, 10), book(0.48, 10, 0.53, 10)
        window.on_books(1.0, up, down)
        self.assertEqual(window.orders[(True, "buy")].price, 0.48)
        self.assertEqual(window.orders[(False, "buy")].price, 0.49)
        window.on_trade(1.01, True, 0.48, 5, "SELL")
        window.on_trade(1.01, False, 0.49, 5, "SELL")
        window.on_books(1.02, up, down)
        self.assertEqual(window.orders[(True, "sell")].price, 0.51)
        self.assertEqual(window.orders[(False, "sell")].price, 0.52)

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
            for side, price, filled_at in ((True, 0.48, 1.1), (False, 0.49, 21.1)):
                fill = window.on_trade(filled_at, side, price, 5, "SELL")
                assert fill is not None
                ledger.record_fill(filled_at, "carry", "btc", window.slug, fill)
            outcomes = [row[0] for row in ledger.db.execute(
                "SELECT outcome_up FROM fills ORDER BY rowid"
            )]
            self.assertEqual(outcomes, [1, 0])
            settled, metrics = window.settle(300.0, 1)
            ledger.record_settlement(300.0, "carry", "btc", window.slug, settled)
            ledger.record_metrics(300.0, "carry", "btc", window.slug, metrics)
            ledger.record_invalid_window(301.0, "rejected", "btc", "bad-slug", {
                "reason": "stale_market_event", "n_fills": 1, "capital": 2.5,
                "cash": -2.5, "up_shares": 5, "down_shares": 0,
                "event_lag_ms": 812,
            })
            ledger.record_fill(301, "carry", "btc", "btc-updown-5m-300", {
                "action": "buy", "price": 0.5, "size": 100,
                "signed_cash": -50, "outcome_up": 1,
            })
            snapshot = report.snapshot_one(ledger.db, "carry")
            self.assertAlmostEqual(snapshot.volume, 4.85)
            self.assertAlmostEqual(snapshot.neutral_pnl, 0.15)
            self.assertAlmostEqual(snapshot.outcome_pnl, 0)
            self.assertAlmostEqual(snapshot.pair_delay_p50_s, 20)
            self.assertAlmostEqual(snapshot.pair_delay_p90_s, 20)
            expected_rebate = (crypto_maker_rebate(0.48, 5)
                               + crypto_maker_rebate(0.49, 5))
            self.assertAlmostEqual(snapshot.maker_rebates, expected_rebate)
            output = report.text(path)
            self.assertIn("carry", output)
            self.assertIn("0.970", output)
            self.assertIn("asset breakdown", output)
            self.assertIn("btc", output)
            self.assertIn("validity=50.0%", output)
            self.assertIn("stale_market_event=1", output)
            self.assertIn("max invalid event lag=812ms", output)
            self.assertIn("lagged", output)
            self.assertIn("812ms", output)
            ledger.close()


if __name__ == "__main__":
    unittest.main()
