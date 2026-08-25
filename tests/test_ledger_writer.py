from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from paper.ledger_writer import LedgerWriter


class LedgerWriterTests(unittest.TestCase):
    def test_background_writer_drains_before_close(self) -> None:
        with TemporaryDirectory() as temp:
            path = str(Path(temp) / "paper.db")
            writer = LedgerWriter(path)
            writer.record_fill(1, "test", "btc", "slug", {
                "action": "sell", "price": 0.6, "size": 5,
                "signed_cash": 3, "outcome_up": 1,
            })
            writer.record_invalid_window(2, "test", "btc", "slug", {
                "reason": "stale_market_event", "n_fills": 1, "capital": 3,
                "cash": -3, "up_shares": 5, "down_shares": 0,
                "event_lag_ms": 812,
            })
            writer.close()
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM fills").fetchone()[0], 1)
                self.assertEqual(
                    db.execute(
                        "SELECT reason,n_fills,capital,event_lag_ms FROM invalid_windows"
                    ).fetchone(),
                    ("stale_market_event", 1, 3.0, 812.0),
                )


if __name__ == "__main__":
    unittest.main()
