from __future__ import annotations

import sqlite3
import unittest

from paper.reference_report import audit, snapshots


class ReferenceReportTests(unittest.TestCase):
    def test_signal_is_causal_and_labels_first_mint_selection(self) -> None:
        start = 1_700_000_000
        db = sqlite3.connect(":memory:")
        db.execute(
            "CREATE TABLE settlements(asset TEXT,slug TEXT,outcome_up INT)"
        )
        db.execute(
            "CREATE TABLE fills(ts REAL,strategy TEXT,slug TEXT,outcome_up INT)"
        )
        db.execute(
            """CREATE TABLE reference_prices(
               asset TEXT, observed_at REAL, received_at REAL,
               value_e18 TEXT, window_s INT)"""
        )
        slug = f"btc-updown-5m-{start}"
        db.execute("INSERT INTO settlements VALUES(?,?,?)", ("btc", slug, 1))
        late_slug = f"btc-updown-5m-{start + 300}"
        db.execute("INSERT INTO settlements VALUES(?,?,?)", ("btc", late_slug, 0))
        db.execute(
            "INSERT INTO fills VALUES(?,?,?,?)",
            (start + 31, "mintcycle5", slug, 1),
        )
        rows = [
            ("btc", start, start + 1.5, "100000000000000000000", 60),
            ("btc", start + 29, start + 29.8, "100500000000000000000", 60),
            # Observed before T+30 but unavailable then: must not leak into the signal.
            ("btc", start + 30, start + 31, "110000000000000000000", 60),
            # Malformed future observation received early: must also be excluded.
            ("btc", start + 31, start + 29, "120000000000000000000", 60),
            # The next opening exists but arrived after its T+30 decision.
            ("btc", start + 300, start + 331, "99000000000000000000", 60),
            ("btc", start + 301, start + 302, "99100000000000000000", 60),
        ]
        db.executemany("INSERT INTO reference_prices VALUES(?,?,?,?,?)", rows)

        result = snapshots(db)

        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].signal_bps, 50)
        self.assertEqual(result[0].source_age_ms, 1000)
        self.assertTrue(result[0].first_mint_sold_up)
        self.assertTrue(result[0].first_mint_toxic)

        reference_audit = audit(db)
        self.assertEqual(reference_audit.total_windows, 2)
        self.assertEqual(reference_audit.signals, tuple(result))
        self.assertEqual(len(reference_audit.misses), 1)
        self.assertEqual(reference_audit.misses[0].reason, "opening_late")
        self.assertEqual(reference_audit.misses[0].nearest_offset_ms, 1000)


if __name__ == "__main__":
    unittest.main()
