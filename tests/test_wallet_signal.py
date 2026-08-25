from __future__ import annotations

import unittest

from tools.wallet_signal import Fill, summarize_signal


class WalletSignalTests(unittest.TestCase):
    def test_snapshot_separates_neutral_edge_from_directional_luck(self) -> None:
        fills = [
            Fill("w", "up-wins", 100, True, True, 110, -6, 10, 6, True),
            Fill("w", "up-wins", 100, True, False, 110, -1.6, 4, 1.6, True),
            Fill("w", "down-wins", 100, False, True, 110, -3, 5, 3, False),
            Fill("w", "down-wins", 100, False, False, 110, -3.5, 5, 3.5, False),
        ]
        result = summarize_signal(fills, "w", 30, min_call_shares=5)
        self.assertEqual((result.markets, result.calls, result.hits), (2, 1, 1))
        self.assertAlmostEqual(result.weighted_alignment, 1.0)
        self.assertAlmostEqual(result.directional_luck, 3.0)
        self.assertAlmostEqual(result.neutral_pnl, -2.1)
        self.assertAlmostEqual(result.actual_pnl, 0.9)
        self.assertAlmostEqual(result.volume, 14.1)
        inconsistent = fills + [
            Fill("w", "up-wins", 100, False, False, 111, 0, 1, 0, True),
        ]
        with self.assertRaisesRegex(ValueError, "inconsistent window metadata"):
            summarize_signal(inconsistent, "w", 30)


if __name__ == "__main__":
    unittest.main()
