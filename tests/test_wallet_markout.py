from __future__ import annotations

import unittest

from tools.wallet_markout import summarize_price, summarize_time
from tools.wallet_signal import Fill


class WalletMarkoutTests(unittest.TestCase):
    def test_terminal_markout_and_taker_fee_are_attributed_once(self) -> None:
        fills = [
            Fill("w", "s", 100, True, True, 110, -6, 10, 6, False, 0.168),
            Fill("w", "s", 100, True, False, 170, 2, -5, 2, True),
        ]
        by_time = summarize_time(fills, "w")
        self.assertAlmostEqual(by_time[("0-60", "taker")].gross_edge, 4)
        self.assertAlmostEqual(by_time[("0-60", "taker")].taker_fee, 0.168)
        by_price = summarize_price(fills, "w", 0, 300)
        self.assertAlmostEqual(by_price[("buy", "taker", "0.50-0.75")].net_edge, 3.832)
        self.assertAlmostEqual(by_price[("sell", "maker", "0.25-0.50")].net_edge, 2)


if __name__ == "__main__":
    unittest.main()
