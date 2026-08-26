from __future__ import annotations

import unittest

from paper.reference_view import ReferenceView


class ReferenceViewCausalityTests(unittest.TestCase):
    """The view decides trades, so peeking ahead would fabricate the result."""

    def _view(self) -> ReferenceView:
        view = ReferenceView()
        for second, value in enumerate((100.0, 101.0, 102.0, 103.0, 104.0)):
            view.update("btc", 1000.0 + second, value)
        return view

    def test_a_future_sample_is_invisible(self) -> None:
        view = self._view()
        self.assertIsNone(view.latest("btc", now=999.0),
                          "nothing was observed before the first sample")
        self.assertEqual(view.latest("btc", now=1002.0), (1002.0, 102.0))
        # the 103/104 samples exist but must not be readable yet
        self.assertEqual(view.latest("btc", now=1002.5), (1002.0, 102.0))

    def test_at_never_returns_a_sample_later_than_now(self) -> None:
        view = self._view()
        self.assertIsNone(
            view.at("btc", 1004.0, now=1001.0),
            "asking for a later timestamp must not leak a future value")
        self.assertEqual(view.at("btc", 1000.0, now=1004.0), 100.0)

    def test_stale_latest_is_refused(self) -> None:
        view = self._view()
        self.assertIsNone(view.latest("btc", now=1100.0, max_age_s=10.0),
                          "a 96s-old sample must not pass as current")
        self.assertIsNotNone(view.latest("btc", now=1100.0, max_age_s=200.0))

    def test_signal_is_bps_against_the_window_opening(self) -> None:
        view = self._view()
        signal = view.signal_bps("btc", 1000.0, now=1004.0)
        self.assertIsNotNone(signal)
        self.assertAlmostEqual(signal, (104.0 / 100.0 - 1) * 10_000)

    def test_signal_is_none_without_an_opening_sample(self) -> None:
        view = ReferenceView()
        view.update("btc", 2000.0, 100.0)
        self.assertIsNone(view.signal_bps("btc", 1000.0, now=2000.0),
                          "no opening value means no signal, not a zero signal")

    def test_signal_is_none_when_the_feed_has_gone_stale(self) -> None:
        view = self._view()
        self.assertIsNone(view.signal_bps("btc", 1000.0, now=1200.0),
                          "a dead feed must not produce a confident signal")

    # -- robustness of the series itself ---------------------------------
    def test_out_of_order_updates_are_ordered(self) -> None:
        view = ReferenceView()
        for ts, value in ((1005.0, 105.0), (1001.0, 101.0), (1003.0, 103.0)):
            view.update("btc", ts, value)
        self.assertEqual(view.latest("btc", now=1005.0), (1005.0, 105.0))
        self.assertEqual(view.at("btc", 1001.0, now=1005.0), 101.0)

    def test_a_repeated_timestamp_replaces_rather_than_duplicates(self) -> None:
        view = ReferenceView()
        view.update("btc", 1000.0, 100.0)
        view.update("btc", 1000.0, 111.0)
        self.assertEqual(view.latest("btc", now=1000.0), (1000.0, 111.0))

    def test_non_positive_values_are_ignored(self) -> None:
        view = ReferenceView()
        view.update("btc", 1000.0, 0.0)
        view.update("btc", 1001.0, -5.0)
        self.assertIsNone(view.latest("btc", now=1001.0))

    def test_old_samples_are_dropped(self) -> None:
        view = ReferenceView(retain_s=10.0)
        view.update("btc", 1000.0, 100.0)
        view.update("btc", 1050.0, 105.0)
        self.assertIsNone(view.at("btc", 1000.0, now=1050.0),
                          "samples beyond the retention window are gone")

    def test_unknown_asset_is_none_everywhere(self) -> None:
        view = self._view()
        self.assertIsNone(view.latest("eth", now=1004.0))
        self.assertIsNone(view.at("eth", 1000.0, now=1004.0))
        self.assertIsNone(view.signal_bps("eth", 1000.0, now=1004.0))




class ReferenceViewRobustnessTests(unittest.TestCase):
    """Found by the review seats; the original 11 tests missed all of these."""

    def test_a_far_future_sample_cannot_erase_history(self) -> None:
        """Retention anchored to the incoming stamp is a free kill switch:
        one skewed frame would wipe the series and blind the strategy."""
        view = ReferenceView(retain_s=900.0)
        for second in range(5):
            view.update("btc", 1000.0 + second, 100.0 + second)
        view.update("btc", 1000.0 + 10_000, 999.0)          # skewed frame
        self.assertEqual(view.latest("btc", now=1004.0), (1004.0, 104.0),
                         "real history must survive an implausible sample")
        self.assertIsNotNone(view.at("btc", 1000.0, now=1004.0))

    def test_non_finite_timestamps_are_rejected(self) -> None:
        view = ReferenceView()
        view.update("btc", 1000.0, 100.0)
        view.update("btc", float("inf"), 500.0)
        view.update("btc", float("nan"), 500.0)
        self.assertEqual(view.latest("btc", now=1e12, max_age_s=1e12),
                         (1000.0, 100.0))

    def test_nan_value_is_rejected(self) -> None:
        view = ReferenceView()
        view.update("btc", 1000.0, float("nan"))
        self.assertIsNone(view.latest("btc", now=1000.0))

    def test_a_sample_stamped_exactly_now_is_visible(self) -> None:
        """Pinning the documented contract: `now` is inclusive."""
        view = ReferenceView()
        view.update("btc", 1000.0, 100.0)
        self.assertEqual(view.latest("btc", now=1000.0), (1000.0, 100.0))


if __name__ == "__main__":
    unittest.main()
