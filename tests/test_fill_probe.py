import json
import sqlite3
import unittest

from paper.fill_quality import snapshots
from paper.fill_probe import FillProbe
from paper.order_book import OrderBook


def book(bid: float, ask: float) -> OrderBook:
    return OrderBook(bids={bid: 5}, asks={ask: 5})


class FillProbeTests(unittest.TestCase):
    def test_signed_markouts_are_sampled_only_after_each_horizon(self) -> None:
        probe = FillProbe()
        probe.record(10, True, "sell", 0.60, 5, 1.2)
        probe.observe(10.99, book(0.62, 0.64), book(0.36, 0.38))
        self.assertFalse(probe.markouts[1])

        probe.observe(11, book(0.62, 0.64), book(0.36, 0.38))
        probe.observe(15, book(0.54, 0.56), book(0.44, 0.46))
        self.assertAlmostEqual(probe.markouts[1][0][0], -0.03)
        self.assertAlmostEqual(probe.markouts[5][0][0], 0.05)
        self.assertEqual(probe.ages, [(1.2, 5)])

    def test_ledger_aggregation_is_share_weighted(self) -> None:
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE window_metrics(strategy TEXT,data TEXT)")
        db.execute("INSERT INTO window_metrics VALUES(?,?)", (
            "mint", json.dumps({
                "maker_fill_ages": [[1, 5], [3, 5]],
                "maker_markouts": {"1": [[-0.03, 5, 0], [0.01, 5, 0]]},
            }),
        ))

        row = snapshots(db)[0]

        self.assertEqual((row.shares, row.age_p50_s, row.age_p90_s), (10, 1, 3))
        self.assertAlmostEqual(row.markout_1_cents, -1)
        self.assertEqual(row.coverage_1, 1)
        self.assertEqual(row.coverage_5, 0)


if __name__ == "__main__":
    unittest.main()
