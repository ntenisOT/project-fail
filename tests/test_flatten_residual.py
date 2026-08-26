from __future__ import annotations

import unittest

from paper.cohort_engine import CohortEngine
from paper.order_book import OrderBook
from paper.pair_engine import PairWindow
from paper.pair_types import PairConfig
from paper.taker import crypto_fee


def _book(bid: float, ask: float, size: float = 50.0) -> OrderBook:
    return OrderBook(bids={bid: size}, asks={ask: size}, min_order_size=5.0)


def _config(flatten: float | None) -> PairConfig:
    return PairConfig(
        "basket99f" if flatten else "basket99", "accumulate", 0.01,
        action_latency_s=0, buy_sum_ceiling=0.99, max_inventory=20,
        flatten_residual_s=flatten,
    )


def _window(flatten: float | None) -> PairWindow:
    return PairWindow(_config(flatten), "btc", "btc-updown-5m-0", 0, "up", "down")


class FlattenResidualTests(unittest.TestCase):
    """The naked residual must be sold for cash, not gambled at settlement.

    Gen91, 11 windows: every arm positive on FIFO-paired edge and deeply
    negative on settlement outcome (basket99 +$3.86 edge vs -$7.21 outcome).
    Four arms do not all lose the coin flip at once - the residual is adversely
    selected. Selling it at a known price and fee makes that a measured cost.
    """

    def test_control_arm_never_flattens(self) -> None:
        window = _window(None)
        window.inventory[True] = 8.0
        window.inventory[False] = 3.0
        records = window._flatten_residual(299.0, _book(0.48, 0.52),
                                           _book(0.47, 0.51))
        self.assertEqual(records, [])
        self.assertEqual(window.inventory[True], 8.0,
                         "the control arm's inventory must be untouched")

    def test_does_nothing_before_the_configured_time(self) -> None:
        window = _window(285.0)
        window.inventory[True] = 8.0
        window.inventory[False] = 3.0
        self.assertEqual(
            window._flatten_residual(284.0, _book(0.48, 0.52), _book(0.47, 0.51)),
            [])
        self.assertFalse(window.flattened)

    def test_sells_only_the_excess_and_leaves_a_balanced_pair(self) -> None:
        window = _window(285.0)
        window.inventory[True] = 8.0
        window.inventory[False] = 3.0
        records = window._flatten_residual(285.0, _book(0.48, 0.52),
                                           _book(0.47, 0.51))
        self.assertTrue(records)
        self.assertAlmostEqual(window.inventory[True], 3.0,
                               msg="must sell exactly the 5-share excess")
        self.assertAlmostEqual(window.inventory[False], 3.0)
        self.assertAlmostEqual(sum(float(r["size"]) for r in records), 5.0)

    def test_flattens_the_down_side_when_that_is_the_excess(self) -> None:
        window = _window(285.0)
        window.inventory[True] = 2.0
        window.inventory[False] = 9.0
        records = window._flatten_residual(285.0, _book(0.48, 0.52),
                                           _book(0.47, 0.51))
        self.assertTrue(records)
        self.assertAlmostEqual(window.inventory[False], 2.0)
        self.assertTrue(all(int(r["outcome_up"]) == 0 for r in records))

    def test_pays_a_real_taker_fee_and_credits_net_cash(self) -> None:
        window = _window(285.0)
        window.inventory[True] = 8.0
        window.inventory[False] = 3.0
        before = window.cash
        records = window._flatten_residual(285.0, _book(0.48, 0.52),
                                           _book(0.47, 0.51))
        expected_fee = crypto_fee(0.48, 5.0)
        self.assertAlmostEqual(window.taker_fees, expected_fee)
        self.assertGreater(expected_fee, 0, "a taker sale is never free")
        gained = sum(float(r["signed_cash"]) for r in records)
        self.assertAlmostEqual(window.cash - before, gained)
        self.assertAlmostEqual(gained, 0.48 * 5.0 - expected_fee)

    def test_runs_once_per_window(self) -> None:
        window = _window(285.0)
        window.inventory[True] = 8.0
        window.inventory[False] = 3.0
        self.assertTrue(window._flatten_residual(285.0, _book(0.48, 0.52),
                                                 _book(0.47, 0.51)))
        window.inventory[True] = 8.0          # a later fill re-opens an imbalance
        self.assertEqual(
            window._flatten_residual(290.0, _book(0.48, 0.52), _book(0.47, 0.51)),
            [], "flatten must not fire repeatedly")

    def test_balanced_inventory_is_a_no_op(self) -> None:
        window = _window(285.0)
        window.inventory[True] = window.inventory[False] = 6.0
        self.assertEqual(
            window._flatten_residual(285.0, _book(0.48, 0.52), _book(0.47, 0.51)),
            [])
        self.assertAlmostEqual(window.inventory[True], 6.0)

    def test_empty_book_is_recorded_as_blocked_not_silently_ignored(self) -> None:
        window = _window(285.0)
        window.inventory[True] = 8.0
        window.inventory[False] = 3.0
        empty = OrderBook(bids={}, asks={}, min_order_size=5.0)
        self.assertEqual(window._flatten_residual(285.0, empty, empty), [])
        self.assertEqual(window.flatten_blocked, 1)
        self.assertAlmostEqual(window.inventory[True], 8.0,
                               "nothing sold means nothing removed")

    def test_records_satisfy_the_engine_fill_contract(self) -> None:
        """A momentum arm once crashed production with a record the engine
        could not read. Every emitted record must survive _fill_record."""
        window = _window(285.0)
        window.inventory[True] = 8.0
        window.inventory[False] = 3.0
        records = window._flatten_residual(285.0, _book(0.48, 0.52),
                                           _book(0.47, 0.51))
        self.assertTrue(records)
        for record in records:
            built = CohortEngine._fill_record(
                285.0, "basket99f", "btc", "btc-updown-5m-0", record)
            self.assertEqual(built.strategy, "basket99f")
            self.assertGreater(built.size, 0)
            self.assertIn(built.outcome_up, (0, 1))


if __name__ == "__main__":
    unittest.main()
