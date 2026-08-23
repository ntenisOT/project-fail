"""Live-readiness gate: lets INDIVIDUAL strategies graduate from paper to live
without touching the trading loop.

Boundary (hard): this module never holds keys, never signs, never submits
orders. It only writes desired-quote INTENTS to paper/intents.jsonl. A separate
executor that the USER runs with their own credentials (e.g. project-magic's
execution engine) consumes that file, diffs desired vs resting orders, and does
the actual placing/cancelling — with its own final risk checks.

Double opt-in before a single intent is written:
  1. env  PAPER_LIVE_INTENTS=1          (off by default)
  2. file paper/live.json               {"enabled": ["twap_deribit"], ...}
Instant stop: create file paper/KILL (checked on every emit).

Risk caps enforced here (first gate, not the last):
  max_order_usd       per-quote notional cap
  max_inventory_usd   per-strategy total exposure cap
  daily_loss_stop_usd realized-pnl stop for the UTC day (from the paper ledger;
                      when live fills flow back, point it at the live ledger)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time

log = logging.getLogger("paper.live_gate")

CONFIG = "paper/live.json"
KILL = "paper/KILL"
INTENTS = "paper/intents.jsonl"
MIN_SHARES = 5.0        # Polymarket minimum limit-order size


class LiveGate:
    def __init__(self, db_path: str = "paper/paper.db"):
        self.db_path = db_path
        self.active = os.environ.get("PAPER_LIVE_INTENTS") == "1"
        self.cfg: dict = {}
        self.cfg_ts = 0.0
        self.pnl_cache: dict[str, float] = {}
        self.pnl_ts = 0.0
        if self.active:
            log.warning("live_gate ACTIVE: intents will be written for strategies enabled in %s", CONFIG)

    # -- config / state -------------------------------------------------
    def _config(self) -> dict:
        if time.time() - self.cfg_ts > 10:
            self.cfg_ts = time.time()
            try:
                with open(CONFIG, encoding="utf-8") as f:
                    self.cfg = json.load(f)
            except (OSError, json.JSONDecodeError):
                self.cfg = {}
        return self.cfg

    def _day_pnl(self, strategy: str) -> float:
        if time.time() - self.pnl_ts > 30:
            self.pnl_ts = time.time()
            day0 = time.time() // 86400 * 86400
            try:
                db = sqlite3.connect(self.db_path)
                self.pnl_cache = dict(db.execute(
                    "SELECT strategy, COALESCE(sum(pnl),0) FROM settlements WHERE ts>=? GROUP BY strategy",
                    (day0,)).fetchall())
                db.close()
            except sqlite3.Error:
                self.pnl_cache = {}
        return self.pnl_cache.get(strategy, 0.0)

    def enabled(self, strategy: str) -> bool:
        return self.active and strategy in (self._config().get("enabled") or [])

    # -- emission -------------------------------------------------------
    def emit_quotes(self, strategy: str, asset: str, slug: str, token: str,
                    is_up: bool, bid, ask, inventory_usd: float) -> None:
        """Write the strategy's DESIRED quote state for one token. The executor
        diffs this against its resting orders. bid/ask None => cancel side."""
        if not self.enabled(strategy) or os.path.exists(KILL):
            return
        cfg = self._config()
        cap_ord = float(cfg.get("max_order_usd", 5.0))
        cap_inv = float(cfg.get("max_inventory_usd", 50.0))
        stop = float(cfg.get("daily_loss_stop_usd", 25.0))
        if self._day_pnl(strategy) <= -stop:
            bid = ask = None                      # loss stop: pull all quotes
        if inventory_usd >= cap_inv:
            bid = None                            # exposure cap: no more buys
        rec = {"ts": round(time.time(), 3), "strategy": strategy, "asset": asset,
               "slug": slug, "token": token, "side_up": bool(is_up),
               "bid": bid, "ask": ask,
               "bid_shares": (max(MIN_SHARES, round(cap_ord / bid, 1)) if bid else 0),
               "ask_shares": (MIN_SHARES if ask else 0),
               "caps": {"order_usd": cap_ord, "inventory_usd": cap_inv, "day_stop_usd": stop}}
        try:
            with open(INTENTS, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            log.warning("intent write failed: %s", e)
