from __future__ import annotations

import unittest

from live.mintbot import PREMIUM_FLOORS, pair_premium_floor


class PairPremiumFloorTests(unittest.TestCase):
    """The ask-pair floor must track the measured pair-premium curve.

    tools/pair_cost_curve.py, 600 BTC windows / 3.9M trades, traded pair sum:
        -180..0s 0.998 | 0..120s 0.998-1.004 | 120..180s 1.017-1.022
        180..240s 1.040-1.068 | 240..300s 1.080-1.109

    The old fixed 1.005 floor was wrong in both directions: above the market
    before T+120 (never lifted) and far below it after T+180 (premium given
    away). These assertions pin the floor to that curve.
    """

    # (elapsed seconds, measured market pair at that point in the window)
    MARKET = ((0, 0.998), (60, 1.000), (119, 1.000),
              (150, 1.017), (200, 1.040), (260, 1.080), (299, 1.109))

    def test_floor_is_inside_the_market_once_a_premium_exists(self) -> None:
        for elapsed, market in self.MARKET:
            floor = pair_premium_floor(elapsed)
            if elapsed >= 120:
                self.assertLess(
                    floor, market,
                    f"at t={elapsed}s the floor {floor} must sit inside the "
                    f"market {market} or the pair can never be lifted")

    def test_never_sells_a_minted_set_below_cost(self) -> None:
        """A $1.00 set sold under $1.00 is a guaranteed loss at any fill rate."""
        for elapsed in (0, 60, 119, 120, 180, 240, 299, 400):
            self.assertGreater(pair_premium_floor(elapsed), 1.0)

    def test_floor_is_monotonically_non_decreasing(self) -> None:
        floors = [pair_premium_floor(t) for t in range(0, 320, 10)]
        self.assertEqual(floors, sorted(floors),
                         "the premium curve rises, so the floor must not fall")

    def test_early_window_floor_clears_cost_without_forgoing_spikes(self) -> None:
        """Early the market averages 0.998-1.004.

        The floor must sit above the 0.998 average (so the routine sub-par
        market cannot lift us at a loss) but NOT above the 1.004 top of the
        band - a pair that briefly trades at 1.004 is worth selling, since the
        set cost $1.00. Refusing that would forgo real money.
        """
        early = pair_premium_floor(60)
        self.assertGreater(early, 1.0, "must never sell a set below its cost")
        self.assertGreater(early, 0.998, "must not be lifted by the sub-par average")
        self.assertLess(early, 1.004, "must still capture an early premium spike")

    def test_boundaries_are_ordered_and_terminal(self) -> None:
        boundaries = [boundary for boundary, _ in PREMIUM_FLOORS]
        self.assertEqual(boundaries, sorted(boundaries))
        # past the last boundary the floor must stay defined, not fall back
        self.assertEqual(pair_premium_floor(10_000), PREMIUM_FLOORS[-1][1])


if __name__ == "__main__":
    unittest.main()
