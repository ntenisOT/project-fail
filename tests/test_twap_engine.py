from __future__ import annotations

import unittest

from paper.cohort_engine import CohortEngine
from paper.order_book import OrderBook
from paper.pair_types import PairConfig
from paper.reference_view import ReferenceView
from paper.twap_engine import TwapWindow


def _book(bid: float, ask: float, size: float = 50.0) -> OrderBook:
    return OrderBook(bids={bid: size}, asks={ask: size}, min_order_size=5.0)


def _config(**over: object) -> PairConfig:
    base: dict[str, object] = dict(
        action_latency_s=0, buy_sum_ceiling=0.99, max_inventory=20,
        clip_shares=5.0, twap_entry_s=240.0, twap_min_bps=1.0,
    )
    base.update(over)
    return PairConfig("twap240", "accumulate", 0.01, **base)  # type: ignore[arg-type]


def _view(*samples: tuple[float, float]) -> ReferenceView:
    view = ReferenceView()
    for observed_at, value in samples:
        view.update("btc", observed_at, value)
    return view


def _window(view: ReferenceView | None, **over: object) -> TwapWindow:
    window = TwapWindow(_config(**over), "btc", "btc-updown-5m-0", 0,
                        "up", "down", reference_view=view)
    # A window whose first books arrive late is invalidated by the engine
    # (late_first_books), so warm it up the way a live window is.
    window.on_books(1.0, _book(0.89, 0.90), _book(0.09, 0.10))
    return window


class TwapEntryTests(unittest.TestCase):
    """The arm both review seats said must be tested here, not backtested."""

    def test_buys_up_when_the_signal_is_positive(self) -> None:
        window = _window(_view((0.0, 100.0), (240.0, 100.5)))
        records = window.on_books(240.0, _book(0.89, 0.90), _book(0.09, 0.10))
        self.assertTrue(records)
        self.assertTrue(all(int(r["outcome_up"]) == 1 for r in records))
        self.assertGreater(window.inventory[True], 0)
        self.assertEqual(window.inventory[False], 0)

    def test_buys_down_when_the_signal_is_negative(self) -> None:
        window = _window(_view((0.0, 100.0), (240.0, 99.5)))
        records = window.on_books(240.0, _book(0.89, 0.90), _book(0.09, 0.10))
        self.assertTrue(records)
        self.assertTrue(all(int(r["outcome_up"]) == 0 for r in records))
        self.assertGreater(window.inventory[False], 0)

    def test_does_nothing_before_the_entry_time(self) -> None:
        window = _window(_view((0.0, 100.0), (239.0, 100.5)))
        self.assertEqual(
            window.on_books(239.0, _book(0.89, 0.90), _book(0.09, 0.10)), [])
        self.assertFalse(window.twap_taken)

    def test_enters_once_only(self) -> None:
        window = _window(_view((0.0, 100.0), (240.0, 100.5), (250.0, 100.9)))
        self.assertTrue(
            window.on_books(240.0, _book(0.89, 0.90), _book(0.09, 0.10)))
        self.assertEqual(
            window.on_books(250.0, _book(0.89, 0.90), _book(0.09, 0.10)), [],
            "the arm takes one shot per window")

    def test_a_flat_signal_is_skipped(self) -> None:
        window = _window(_view((0.0, 100.0), (240.0, 100.0000001)))
        self.assertEqual(
            window.on_books(240.0, _book(0.89, 0.90), _book(0.09, 0.10)), [])

    # -- the causality property the review seats attacked -----------------
    def test_a_sample_that_has_not_arrived_yet_is_not_used(self) -> None:
        """The killer objection was lookahead: on the live feed every sample
        arrives a median 1.678s after the moment it describes. A sample the
        feed has not published must be invisible."""
        view = ReferenceView()
        view.update("btc", 0.0, 100.0)
        view.update("btc", 241.7, 100.5)      # observed at 241.7, not 240
        window = _window(view)
        self.assertEqual(
            window.on_books(240.0, _book(0.89, 0.90), _book(0.09, 0.10)), [],
            "entry must not use a sample published after the decision moment")
        self.assertEqual(window.twap_no_signal, 1)
        self.assertTrue(
            window.on_books(241.7, _book(0.89, 0.90), _book(0.09, 0.10)),
            "once it has arrived the arm may act")

    def test_a_stale_feed_produces_no_trade(self) -> None:
        window = _window(_view((0.0, 100.0), (100.0, 100.5)))
        self.assertEqual(
            window.on_books(240.0, _book(0.89, 0.90), _book(0.09, 0.10)), [],
            "a 140s-old sample must not be treated as current")
        self.assertEqual(window.twap_no_signal, 1)

    def test_no_view_means_no_trade(self) -> None:
        window = _window(None)
        self.assertEqual(
            window.on_books(240.0, _book(0.89, 0.90), _book(0.09, 0.10)), [])

    # -- execution realism ------------------------------------------------
    def test_pays_a_taker_fee_and_is_limited_to_displayed_depth(self) -> None:
        window = _window(_view((0.0, 100.0), (240.0, 100.5)), clip_shares=10.0)
        thin = OrderBook(bids={0.89: 50}, asks={0.90: 6.0}, min_order_size=5.0)
        records = window.on_books(240.0, thin, _book(0.09, 0.10))
        self.assertTrue(records)
        self.assertLessEqual(sum(float(r["size"]) for r in records), 6.0,
                             "cannot buy more than the book displays")
        self.assertGreater(window.taker_fees, 0, "crossing is never free")

    def test_depth_below_the_minimum_order_size_is_untradeable(self) -> None:
        """The backtest ignored orderMinSize; the engine does not. Depth of 2
        shares against a 5-share minimum is not a small fill, it is no fill -
        which is one reason the backtested edge was overstated."""
        window = _window(_view((0.0, 100.0), (240.0, 100.5)))
        thin = OrderBook(bids={0.89: 50}, asks={0.90: 2.0}, min_order_size=5.0)
        self.assertEqual(window.on_books(240.0, thin, _book(0.09, 0.10)), [])
        self.assertEqual(window.twap_blocked, 1)

    def test_an_empty_book_is_recorded_as_blocked(self) -> None:
        window = _window(_view((0.0, 100.0), (240.0, 100.5)))
        empty = OrderBook(bids={}, asks={}, min_order_size=5.0)
        self.assertEqual(window.on_books(240.0, empty, _book(0.09, 0.10)), [])
        self.assertEqual(window.twap_blocked, 1)

    def test_it_never_rests_a_quote(self) -> None:
        window = _window(_view((0.0, 100.0), (240.0, 100.5)))
        self.assertEqual(window._desired(100.0, _book(0.89, 0.90),
                                         _book(0.09, 0.10)), {})

    def test_records_satisfy_the_engine_fill_contract(self) -> None:
        """A momentum arm once killed a live cohort with a malformed record."""
        window = _window(_view((0.0, 100.0), (240.0, 100.5)))
        for record in window.on_books(240.0, _book(0.89, 0.90),
                                      _book(0.09, 0.10)):
            built = CohortEngine._fill_record(
                240.0, "twap240", "btc", "btc-updown-5m-0", record)
            self.assertGreater(built.size, 0)
            self.assertIn(built.outcome_up, (0, 1))


if __name__ == "__main__":
    unittest.main()
