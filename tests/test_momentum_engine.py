from __future__ import annotations

import unittest

from paper.momentum_engine import MomentumWindow
from paper.order_book import OrderBook
from paper.pair_types import PairConfig


def book(bid: float, ask: float, size: float = 500.0) -> OrderBook:
    return OrderBook(bids={bid: size}, asks={ask: size}, min_order_size=5.0)


def config(**overrides: object) -> PairConfig:
    base = dict(
        clip_shares=6.0, max_inventory=30.0, momentum_threshold=0.10,
        momentum_lookback_s=10.0, momentum_hold_s=30.0, new_pair_start_s=0,
        action_latency_s=0.065,
    )
    base.update(overrides)
    return PairConfig("mom", "momentum", 0.02, **base)  # type: ignore[arg-type]


class MomentumWindowTests(unittest.TestCase):
    def _window(self, **overrides: object) -> MomentumWindow:
        return MomentumWindow(config(**overrides), "btc", "btc-updown-5m-1000",
                              1000, "UP", "DN", 1000)

    def test_enters_after_a_qualifying_move_and_exits_after_the_hold(self) -> None:
        window = self._window()
        flat = book(0.49, 0.51)
        # establish a reference mid, then move the Up side up by 12 cents
        window.on_books(1001, flat, flat)
        window.on_books(1012, book(0.61, 0.63), flat)
        self.assertEqual(window.momentum_entries, 1, "a >=10c move must trigger entry")
        self.assertGreater(window.inventory[True], 0.0)
        self.assertLess(window.cash, 0.0, "a taker buy must spend cash")
        self.assertGreater(window.taker_fees, 0.0, "taker entry must pay a fee")

        # before the hold elapses it must not exit
        window.on_books(1030, book(0.66, 0.68), flat)
        self.assertEqual(window.momentum_exits, 0)
        # after the hold it exits by hitting the bid
        window.on_books(1045, book(0.66, 0.68), flat)
        self.assertEqual(window.momentum_exits, 1)
        self.assertAlmostEqual(window.inventory[True], 0.0, places=6)

    def test_no_entry_below_threshold(self) -> None:
        window = self._window()
        flat = book(0.49, 0.51)
        window.on_books(1001, flat, flat)
        window.on_books(1012, book(0.53, 0.55), flat)   # only a 4c move
        self.assertEqual(window.momentum_entries, 0)
        self.assertEqual(window.inventory[True], 0.0)

    def test_never_rests_a_quote(self) -> None:
        """Momentum takes liquidity; it must never post a maker order."""
        window = self._window()
        flat = book(0.49, 0.51)
        for t in range(1001, 1040):
            window.on_books(t, flat, flat)
        self.assertEqual(window.orders, {})
        self.assertEqual(window.quote_posts, 0)
        self.assertEqual(window.maker_rebates, 0.0)

    def test_refuses_entry_it_cannot_close_before_settlement(self) -> None:
        """Never open a position whose hold would run past the window end."""
        window = self._window(momentum_hold_s=600.0)
        flat = book(0.49, 0.51)
        window.on_books(1001, flat, flat)
        window.on_books(1012, book(0.61, 0.63), flat)
        self.assertEqual(window.momentum_entries, 0)
        self.assertEqual(window.inventory[True], 0.0)

    def test_position_is_flat_before_the_window_ends(self) -> None:
        window = self._window(momentum_hold_s=200.0)
        flat = book(0.49, 0.51)
        window.on_books(1001, flat, flat)
        window.on_books(1012, book(0.61, 0.63), flat)
        self.assertEqual(window.momentum_entries, 1)
        # no books arrive for a while; the next one is inside the final 15s
        window.on_books(1290, book(0.61, 0.63), flat)
        self.assertEqual(window.momentum_exits, 1, "must flatten before settlement")
        self.assertAlmostEqual(window.inventory[True], 0.0, places=6)
        settlement, _ = window.settle(1300, 1)
        self.assertAlmostEqual(settlement["resid_shares"], 0.0, places=6)

    def test_settles_without_violating_the_inventory_invariant(self) -> None:
        window = self._window()
        flat = book(0.49, 0.51)
        window.on_books(1001, flat, flat)
        window.on_books(1012, book(0.61, 0.63), flat)
        window.on_books(1045, book(0.66, 0.68), flat)
        settlement, metrics = window.settle(1300, 1)
        self.assertEqual(settlement["buys"], 1)
        self.assertEqual(settlement["sells"], 1)
        self.assertIn("pnl", settlement)
        self.assertGreater(metrics["taker_fees"], 0.0)

    def test_profitable_round_trip_makes_money_net_of_fees(self) -> None:
        window = self._window()
        flat = book(0.49, 0.51)
        window.on_books(1001, flat, flat)
        window.on_books(1012, book(0.61, 0.63), flat)   # buy the 0.63 ask
        window.on_books(1045, book(0.80, 0.82), flat)   # sell the 0.80 bid
        settlement, _ = window.settle(1300, 0)
        self.assertEqual(window.momentum_exits, 1)
        # 0.80 - 0.63 = 0.17/share gross, far above the ~0.035 round-trip fee
        self.assertGreater(settlement["pnl"], 0.0)


if __name__ == "__main__":
    unittest.main()
