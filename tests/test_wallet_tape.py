from __future__ import annotations

import unittest

from tools.wallet_signal import Fill
from tools.market_windows import ResolvedWindow
from tools.wallet_tape import TapeMark, summarize_alignment, summarize_tape_edge


class WalletTapeTests(unittest.TestCase):
    def test_counts_incremental_calls_only_when_wallet_and_tape_disagree(self) -> None:
        fills = [
            Fill("w", "agree", 100, True, True, 110, 0, 10, 0, True),
            Fill("w", "disagree", 100, False, True, 110, 0, 10, 0, True),
        ]
        marks = [TapeMark("agree", 30, 0.6), TapeMark("disagree", 30, 0.4)]
        result = summarize_alignment(fills, marks, "w", 30)
        self.assertEqual(
            (result.calls, result.wallet_hits, result.tape_hits, result.agreements,
             result.disagreements, result.wallet_disagreement_wins),
            (2, 1, 2, 1, 1, 0),
        )
        windows = [
            ResolvedWindow("btc-updown-5m-100", "btc", 100, "0x" + "1" * 64,
                           "1", "2", 1),
            ResolvedWindow("eth-updown-5m-100", "eth", 100, "0x" + "2" * 64,
                           "3", "4", 0),
        ]
        edge_marks = [TapeMark(windows[0].slug, 30, 0.6),
                      TapeMark(windows[1].slug, 30, 0.4)]
        edge = summarize_tape_edge(edge_marks, windows, 30, slippage=0)
        self.assertEqual((edge.calls, edge.wins), (2, 2))
        self.assertAlmostEqual(edge.average_price, 0.6)
        self.assertAlmostEqual(edge.ev_per_share, 0.3832)


if __name__ == "__main__":
    unittest.main()
