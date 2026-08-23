"""Isolated SQLite ledger for the paper trader — strategy-tagged for A/B comparison."""
from __future__ import annotations

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
              action TEXT, price REAL, size REAL, signed_cash REAL);
            CREATE TABLE IF NOT EXISTS settlements(
              ts REAL, strategy TEXT, asset TEXT, slug TEXT, cash REAL, residual REAL,
              pnl REAL, capital REAL, buys INT, sells INT, resid_shares REAL,
              n_fills INT, outcome_up INT);
            """
        )
        self.db.commit()

    def record_fill(self, ts, strategy, asset, slug, rec) -> None:
        self.db.execute(
            "INSERT INTO fills VALUES(?,?,?,?,?,?,?,?)",
            (ts, strategy, asset, slug, rec["action"], rec["price"], rec["size"], rec["signed_cash"]),
        )
        self.db.commit()

    def record_settlement(self, ts, strategy, asset, slug, s) -> None:
        self.db.execute(
            "INSERT INTO settlements VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, strategy, asset, slug, s["cash"], s["residual"], s["pnl"], s["capital"],
             s["buys"], s["sells"], s["resid_shares"], s["n_fills"], int(s["outcome_up"])),
        )
        self.db.commit()
