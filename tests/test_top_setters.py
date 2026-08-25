from __future__ import annotations

import unittest

from tools.clickhouse_forensics import _legs_sql
from tools.market_windows import parse_gamma_event
from tools.wallet_metrics import TokenActivity, summarize_wallets


def activity(
    wallet: str,
    slug: str,
    asset: str,
    start: int,
    side: int,
    *,
    bought: float = 0.0,
    sold: float = 0.0,
    pnl: float = 0.0,
    volume: float = 0.0,
    maker_volume: float = 0.0,
    buy_fee: float = 0.0,
    sell_fee: float = 0.0,
) -> TokenActivity:
    return TokenActivity(
        wallet=wallet,
        slug=slug,
        asset=asset,
        start=start,
        side=side,
        pnl=pnl,
        volume=volume,
        bought=bought,
        buy_usdc=bought * 0.5,
        sold=sold,
        sell_usdc=sold * 0.5,
        net_shares=bought - sold,
        maker_volume=maker_volume,
        fills=1,
        maker_fills=1 if maker_volume else 0,
        buy_fee=buy_fee,
        sell_fee=sell_fee,
    )


class WalletAggregationTests(unittest.TestCase):
    def test_v2_fill_rows_emit_only_the_order_owner_leg(self) -> None:
        v2_branch, legacy_buy, legacy_sell = _legs_sql(1, 2).split("UNION ALL")
        self.assertIn("SELECT maker AS wallet", v2_branch)
        self.assertIn("AND tx_hash IN v2_transactions", v2_branch)
        self.assertIn("lower(taker) NOT IN", v2_branch)
        self.assertNotIn("maker, taker) AS wallet", v2_branch)
        self.assertIn("AND tx_hash NOT IN v2_transactions", legacy_buy)
        self.assertIn("AND tx_hash NOT IN v2_transactions", legacy_sell)

    def test_same_timestamp_different_assets_is_not_both_sides(self) -> None:
        rows = [
            activity("0xABC", "btc-updown-5m-300", "btc", 300, 1, bought=5),
            activity("0xABC", "eth-updown-5m-300", "eth", 300, 0, bought=5),
        ]
        result = summarize_wallets(rows)[0]
        self.assertEqual(result.market_windows, 2)
        self.assertEqual(result.both_windows, 0)
        self.assertEqual(result.both_pct, 0.0)

    def test_book_buys_cannot_mask_other_token_inventory_deficit(self) -> None:
        rows = [
            activity("w", "btc-updown-5m-300", "btc", 300, 1, sold=10),
            activity("w", "btc-updown-5m-300", "btc", 300, 0, bought=10),
        ]
        result = summarize_wallets(rows)[0]
        self.assertEqual(result.sold, 10.0)
        self.assertEqual(result.inventory_floor_shares, 10.0)
        self.assertEqual(result.inventory_floor_pct, 100.0)

    def test_both_buy_and_sell_metrics_are_market_scoped(self) -> None:
        rows = [
            activity("W", "btc-updown-5m-300", "btc", 300, 1,
                     bought=12, sold=10, pnl=3, volume=11, maker_volume=8),
            activity("W", "btc-updown-5m-300", "btc", 300, 0,
                     bought=10, sold=12, pnl=2, volume=9, maker_volume=7),
        ]
        result = summarize_wallets(rows, {("w", "btc-updown-5m-300"): (20, 4)})[0]
        self.assertEqual(result.market_windows, 1)
        self.assertEqual(result.both_pct, 100.0)
        self.assertEqual(result.bought_both_pct, 100.0)
        self.assertEqual(result.sold_both_pct, 100.0)
        self.assertEqual(result.pnl, 5.0)
        self.assertEqual(result.direct_split_sets, 20.0)
        self.assertEqual(result.direct_merge_sets, 4.0)
        self.assertAlmostEqual(result.maker_share_pct, 75.0)
        self.assertEqual(result.buy_pair_sum, 1.0)
        self.assertEqual(result.sell_pair_sum, 1.0)

    def test_duplicate_side_fails_closed(self) -> None:
        row = activity("w", "btc-updown-5m-300", "btc", 300, 1, bought=5)
        with self.assertRaisesRegex(ValueError, "duplicate per-token"):
            summarize_wallets([row, row])

    def test_pair_prices_and_pnl_account_for_taker_fees(self) -> None:
        rows = [
            activity("w", "btc-updown-5m-300", "btc", 300, side,
                     bought=10, buy_fee=0.1)
            for side in (0, 1)
        ]
        result = summarize_wallets(rows)[0]
        self.assertAlmostEqual(result.buy_pair_sum or 0, 1.02)
        self.assertAlmostEqual(result.taker_fees, 0.2)


class GammaParsingTests(unittest.TestCase):
    def test_nonmatching_gamma_market_is_not_relabelled(self) -> None:
        payload = [{"markets": [{
            "slug": "eth-updown-5m-300",
            "closed": True,
            "conditionId": "0x" + "a" * 64,
            "outcomes": ["Up", "Down"],
            "outcomePrices": ["1", "0"],
            "clobTokenIds": ["11", "22"],
        }]}]
        self.assertIsNone(parse_gamma_event("btc", 300, payload))

    def test_outcome_labels_determine_token_order(self) -> None:
        payload = [{"markets": [{
            "slug": "btc-updown-5m-300",
            "closed": True,
            "conditionId": "0x" + "a" * 64,
            "outcomes": '["Down", "Up"]',
            "outcomePrices": '["0", "1"]',
            "clobTokenIds": '["22", "11"]',
        }]}]
        result = parse_gamma_event("btc", 300, payload)
        assert result is not None
        self.assertEqual(result.up_token, "11")
        self.assertEqual(result.down_token, "22")
        self.assertEqual(result.winner_up, 1)

    def test_unresolved_market_is_not_scored(self) -> None:
        payload = [{"markets": [{
            "slug": "btc-updown-5m-300",
            "closed": False,
            "conditionId": "0x" + "a" * 64,
            "outcomes": ["Up", "Down"],
            "outcomePrices": ["0.55", "0.45"],
            "clobTokenIds": ["11", "22"],
        }]}]
        self.assertIsNone(parse_gamma_event("btc", 300, payload))


if __name__ == "__main__":
    unittest.main()
