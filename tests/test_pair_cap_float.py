from __future__ import annotations

import unittest

from paper.pair_engine import _exceeds_cap


class ExactFitTickPairTests(unittest.TestCase):
    """A pair whose ticks sum exactly to the ceiling must be allowed to quote.

    Tick prices are not exactly representable in binary. 0.02 + 0.93 evaluates
    to 0.9500000000000001, so a bare `> cap` refuses a pair that exactly meets
    a 0.95 ceiling. That silently biases the lower-ceiling arms toward not
    quoting, which corrupts the selectivity gradient basket95/97/99 exist to
    measure. Found by the Qwen review seat.
    """

    CAPS = (0.95, 0.97, 0.99, 1.00)

    def _exact_fit_pairs(self, cap: float) -> list[tuple[float, float]]:
        pairs = []
        for i in range(1, 100):
            a = i / 100
            b = round(cap - a, 10)
            if 0 < b < 1:
                pairs.append((round(a, 2), round(b, 2)))
        return pairs

    def test_no_exact_fit_pair_is_rejected_at_any_ceiling(self) -> None:
        rejected = [
            (cap, a, b)
            for cap in self.CAPS
            for a, b in self._exact_fit_pairs(cap)
            if _exceeds_cap(a + b, cap)
        ]
        self.assertEqual(rejected, [],
                         "pairs that exactly meet the ceiling must be quotable")

    def test_the_specific_pairs_float_error_used_to_reject(self) -> None:
        for cap, a, b in ((0.95, 0.02, 0.93), (0.95, 0.27, 0.68),
                          (0.95, 0.39, 0.56)):
            self.assertGreater(a + b, cap, "precondition: float sum is high")
            self.assertFalse(_exceeds_cap(a + b, cap),
                             f"{a}+{b} exactly meets {cap} and must be allowed")

    def test_a_pair_genuinely_over_the_cap_is_still_rejected(self) -> None:
        """The tolerance must not let a real tick through."""
        self.assertTrue(_exceeds_cap(1.01, 0.99), "one tick over must be refused")
        self.assertTrue(_exceeds_cap(0.96, 0.95))
        self.assertTrue(_exceeds_cap(0.9501, 0.95),
                        "even a sub-tick real excess must be refused")

    def test_a_pair_under_the_cap_is_allowed(self) -> None:
        self.assertFalse(_exceeds_cap(0.98, 0.99))
        self.assertFalse(_exceeds_cap(0.50, 0.99))


if __name__ == "__main__":
    unittest.main()
