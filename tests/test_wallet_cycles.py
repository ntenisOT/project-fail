from __future__ import annotations

import unittest

from tools.wallet_cycles import Fill, summarize_cycles, summarize_inventory_cycles


class WalletCycleTests(unittest.TestCase):
    def test_only_later_observed_set_sales_count_as_cycles(self) -> None:
        wallet, slug = "0xabc", "btc-updown-5m-0"
        fills = [
            Fill(wallet, slug, 1, (1, 1), 1, False, 5, 0.55),
            Fill(wallet, slug, 0, (1, 2), 2, False, 5, 0.55),
            Fill(wallet, slug, 1, (2, 1), 3, True, 5, 0.40),
            Fill(wallet, slug, 0, (2, 2), 4, True, 5, 0.50),
            Fill(wallet, slug, 1, (3, 1), 8, False, 5, 0.52),
            Fill(wallet, slug, 0, (3, 2), 9, False, 5, 0.58),
            Fill(wallet, "eth-updown-5m-0", 1, (4, 1), 10, True, 5, 0.40),
            Fill(wallet, "eth-updown-5m-0", 1, (4, 2), 11, False, 5, 0.60),
            Fill(wallet, "eth-updown-5m-0", 0, (4, 3), 12, True, 5, 0.40),
            Fill(wallet, "eth-updown-5m-0", 0, (4, 4), 13, False, 5, 0.60),
        ]
        result = summarize_cycles(fills, wallet)
        self.assertEqual((result.markets, result.cycle_markets, result.shares), (2, 1, 5))
        self.assertAlmostEqual(result.buy_sum or 0, 0.90)
        self.assertAlmostEqual(result.sell_sum or 0, 1.10)
        self.assertAlmostEqual(result.edge, 1.0)
        self.assertEqual(result.uncovered_sell_shares, 10)
        self.assertEqual(result.median_hold_s, 5)

    def test_same_token_cycles_net_sell_first_and_buy_first_lots_once(self) -> None:
        wallet, slug = "0xabc", "btc-updown-5m-0"
        fills = [
            Fill(wallet, slug, 1, (1, 1), 1, False, 5, 0.60, True),
            Fill(wallet, slug, 1, (2, 1), 2, True, 3, 0.50, True),
            Fill(wallet, slug, 1, (3, 1), 3, True, 4, 0.55, True),
            Fill(wallet, slug, 1, (4, 1), 4, False, 2, 0.65, True),
        ]
        result = summarize_inventory_cycles(fills, wallet)
        self.assertEqual((result.markets, result.cycle_markets, result.shares), (1, 1, 7))
        self.assertAlmostEqual(result.edge, 0.6)
        self.assertEqual(result.sell_first_shares, 5)
        self.assertEqual(result.maker_shares, 7)
        self.assertEqual(result.open_shares, 0)
        self.assertEqual(result.median_hold_s, 1)


if __name__ == "__main__":
    unittest.main()
