"""Isolated SQLite ledger for the paper trader — strategy-tagged for A/B comparison."""
from __future__ import annotations

import json
import os
import sqlite3


class Ledger:
    def __init__(self, path: str = "paper/paper.db"):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS fills(
              ts REAL, strategy TEXT, asset TEXT, slug TEXT,
              action TEXT, price REAL, size REAL, signed_cash REAL, outcome_up INT);
            CREATE TABLE IF NOT EXISTS settlements(
              ts REAL, strategy TEXT, asset TEXT, slug TEXT, cash REAL, residual REAL,
              pnl REAL, capital REAL, buys INT, sells INT, resid_shares REAL,
              n_fills INT, outcome_up INT);
            CREATE TABLE IF NOT EXISTS window_metrics(
              ts REAL, strategy TEXT, asset TEXT, slug TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS invalid_windows(
              ts REAL, strategy TEXT, asset TEXT, slug TEXT, reason TEXT,
              n_fills INT, capital REAL, cash REAL, up_shares REAL, down_shares REAL,
              event_lag_ms REAL);
            CREATE TABLE IF NOT EXISTS reference_prices(
              asset TEXT, observed_at REAL, received_at REAL,
              value_e18 TEXT, window_s INT,
              PRIMARY KEY(asset,observed_at,window_s));
            """
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(fills)")}
        if "outcome_up" not in columns:
            self.db.execute("ALTER TABLE fills ADD COLUMN outcome_up INT")
        invalid_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(invalid_windows)")
        }
        if "event_lag_ms" not in invalid_columns:
            self.db.execute("ALTER TABLE invalid_windows ADD COLUMN event_lag_ms REAL")
        self.db.commit()

    def record_metrics(self, ts, strategy, asset, slug, metrics) -> None:
        self.db.execute(
            "INSERT INTO window_metrics VALUES(?,?,?,?,?)",
            (ts, strategy, asset, slug, json.dumps(metrics, sort_keys=True)),
        )
        self.db.commit()

    def record_invalid_window(self, ts, strategy, asset, slug, invalid) -> None:
        self.db.execute(
            """INSERT INTO invalid_windows(
                 ts,strategy,asset,slug,reason,n_fills,capital,cash,up_shares,down_shares,
                 event_lag_ms
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, strategy, asset, slug, invalid["reason"], invalid["n_fills"],
             invalid["capital"], invalid["cash"], invalid["up_shares"],
             invalid["down_shares"], invalid["event_lag_ms"]),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def record_fill(self, ts, strategy, asset, slug, rec) -> None:
        self.db.execute(
            """INSERT INTO fills(
                 ts,strategy,asset,slug,action,price,size,signed_cash,outcome_up
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (ts, strategy, asset, slug, rec["action"], rec["price"], rec["size"],
             rec["signed_cash"], rec["outcome_up"]),
        )
        self.db.commit()

    def record_reference(self, asset: str, observed_at: float, received_at: float,
                         value_e18: str, window_s: int) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO reference_prices VALUES(?,?,?,?,?)",
            (asset, observed_at, received_at, value_e18, window_s),
        )
        self.db.commit()

    def record_settlement(self, ts, strategy, asset, slug, s) -> None:
        self.db.execute(
            "INSERT INTO settlements VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, strategy, asset, slug, s["cash"], s["residual"], s["pnl"], s["capital"],
             s["buys"], s["sells"], s["resid_shares"], s["n_fills"], int(s["outcome_up"])),
        )
        self.db.commit()
