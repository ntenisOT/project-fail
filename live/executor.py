"""Live executor: consumes paper/intents.jsonl (desired quotes from the paper
runner's LiveGate) and maintains real resting limit orders on the Polymarket
CLOB. Run it YOURSELF:

    python -m live.executor

Modes (LIVE_EXECUTOR_MODE in .env):
  log-only : full reconcile loop, prints every order it WOULD place/cancel.
             No credentials needed. Prove the loop like this first.
  place    : REAL ORDERS. Needs POLY_PRIVATE_KEY (+ POLY_FUNDER for site
             accounts) in .env AND DEPLOY_REGION=eu-west-1 (geo interlock).

Guards (hardened audit 2026-08-23):
  G1  place-mode geo interlock (DEPLOY_REGION=eu-west-1)
  G2  startup cancel_all + cancel_all on EVERY exit path (no orphaned orders)
  G3  per-action try/except: one API rejection never kills the loop
  G4  window-end hard cancel: every quote dies >=2s before its market closes
      (independent of intent flow), plus 240s stale-intent fallback
  G5  order cap strict: if max_order_usd cannot fit a 5-share order at the
      price, nothing is posted (never exceeds the $ cap)
  G6  sells only against REAL holdings (position tracked from live fills);
      never posts an ask for tokens we do not hold
  G7  global inventory cap: open exposure (net cash outflow) capped at
      n_enabled * max_inventory_usd -> no new bids beyond it
  G8  day loss stop on REALIZED basis: halts + cancels when net cash outflow
      exceeds (total inventory cap + daily_loss_stop_usd) - i.e. money is
      actually gone, not merely deployed
  G9  rate budget: max ACTIONS_PER_LOOP order actions per 1s loop
  G10 KILL file (paper/KILL): cancel everything and exit, checked every loop
  G11 intents-file truncation/rotation detected (read position reset)

Attribution note: enabled strategies quote the SAME market tokens, so live
fills cannot be split per strategy - caps and PnL are enforced GLOBALLY here.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time

from paper import envload

envload.load()
logging.basicConfig(level=logging.INFO, format="%(asctime)s live.exec %(message)s")
log = logging.getLogger("live.executor")

INTENTS = "paper/intents.jsonl"
CONFIG = "paper/live.json"
KILL = "paper/KILL"
DB = "live/live.db"
MODE = os.environ.get("LIVE_EXECUTOR_MODE", "log-only")
MIN_SHARES = 5.0
TICK = 0.01
WINDOW_S = 300           # 5m markets; slug carries the window base timestamp
CLOSE_EARLY_S = 2.0      # cancel this many seconds before market close
STALE_INTENT_S = 240.0
ACTIONS_PER_LOOP = 8     # order-action budget per ~1s loop (rate guard)
FILLS_EVERY_S = 15.0
HOST = "https://clob.polymarket.com"


def cfg() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def slug_close_ts(slug: str):
    """{asset}-updown-5m-{base} -> base + 300 (window close, unix)."""
    try:
        return int(slug.rsplit("-", 1)[1]) + WINDOW_S
    except (ValueError, IndexError):
        return None


class Ledger:
    def __init__(self):
        os.makedirs("live", exist_ok=True)
        self.db = sqlite3.connect(DB)
        self.db.execute("""CREATE TABLE IF NOT EXISTS live_fills(
            ts REAL, strategy TEXT, token TEXT, side TEXT, price REAL, size REAL,
            usd REAL, trade_id TEXT UNIQUE)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS live_orders(
            ts REAL, strategy TEXT, token TEXT, side TEXT, price REAL, size REAL,
            order_id TEXT, action TEXT)""")
        self.db.commit()

    def order(self, strategy, token, side, price, size, oid, action):
        try:
            self.db.execute("INSERT INTO live_orders VALUES (?,?,?,?,?,?,?,?)",
                            (time.time(), strategy, token, side, price, size, oid, action))
            self.db.commit()
        except sqlite3.Error as e:
            log.warning("order write: %s", e)

    def fill(self, rec):
        try:
            self.db.execute("INSERT OR IGNORE INTO live_fills VALUES (?,?,?,?,?,?,?,?)", rec)
            self.db.commit()
        except sqlite3.Error as e:
            log.warning("fill write: %s", e)

    def positions_and_cash(self, day0: float):
        """(pos: token->net shares from ALL fills, day net cash from today's fills)"""
        pos = {}
        for tok, sh in self.db.execute(
                "SELECT token, SUM(CASE WHEN side='BUY' THEN size ELSE -size END) FROM live_fills GROUP BY token"):
            pos[tok] = sh or 0.0
        cash = self.db.execute(
            "SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN -usd ELSE usd END),0) FROM live_fills WHERE ts>=?",
            (day0,)).fetchone()[0]
        return pos, float(cash)


class Clob:
    """Thin wrapper; only constructed in place mode. All calls raise -> callers wrap."""
    def __init__(self):
        from py_clob_client.client import ClobClient
        key = os.environ["POLY_PRIVATE_KEY"]
        funder = os.environ.get("POLY_FUNDER") or None
        sig = int(os.environ.get("POLY_SIGNATURE_TYPE", "2"))
        kw = {"key": key, "chain_id": 137}
        if funder:
            kw.update({"signature_type": sig, "funder": funder})
        self.c = ClobClient(HOST, **kw)
        self.c.set_api_creds(self.c.create_or_derive_api_creds())
        self.addr = (self.c.get_address() or "").lower()
        log.info("CLOB client ready (addr %s...%s)", self.addr[:6], self.addr[-4:])

    def place(self, token, side, price, size):
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        args = OrderArgs(price=round(price, 2), size=round(size, 1),
                         side=BUY if side == "buy" else SELL, token_id=token)
        r = self.c.post_order(self.c.create_order(args), OrderType.GTC)
        if isinstance(r, dict) and not r.get("success", True):
            raise RuntimeError(f"post_order rejected: {r.get('errorMsg', r)}")
        return (r or {}).get("orderID")

    def cancel(self, oid):
        self.c.cancel(oid)

    def cancel_all(self):
        self.c.cancel_all()

    def trades(self):
        from py_clob_client.clob_types import TradeParams
        return self.c.get_trades(TradeParams(maker_address=self.addr)) or []

    def my_side(self, t: dict) -> str:
        """Trade rows report the TAKER side; flip it when we were the maker."""
        side = str(t.get("side", "")).upper()
        if str(t.get("maker_address", "")).lower() == self.addr:
            return "SELL" if side == "BUY" else "BUY"
        return side


def main():
    conf = cfg()
    enabled = set(conf.get("enabled") or [])
    cap_ord = float(conf.get("max_order_usd", 5.0))
    cap_inv_total = float(conf.get("max_inventory_usd", 50.0)) * max(1, len(enabled))  # G7 global
    stop = float(conf.get("daily_loss_stop_usd", 25.0))
    log.info("mode=%s enabled=%s | caps: $%.0f/order, $%.0f open total, day-stop realized $%.0f",
             MODE, sorted(enabled), cap_ord, cap_inv_total, stop)
    if not enabled:
        log.error("no strategies enabled in %s - nothing to do", CONFIG)
        return

    clob = None
    if MODE == "place":
        if os.environ.get("DEPLOY_REGION") != "eu-west-1":            # G1
            log.error("REFUSED: place mode requires DEPLOY_REGION=eu-west-1 in .env "
                      "(this box is paper-only; deploy per GO_LIVE.md)")
            return
        clob = Clob()
        try:
            clob.cancel_all()                                          # G2: clean slate
            log.info("startup cancel_all done (no orphans from a previous run)")
        except Exception as e:
            log.error("startup cancel_all FAILED (%s) - refusing to run blind", e)
            return
    else:
        log.info("LOG-ONLY: no orders will be sent")

    led = Ledger()
    desired: dict[tuple, dict] = {}       # (strategy, token) -> latest intent
    resting: dict[tuple, dict] = {}       # (strategy, token, side) -> {id, price, size}
    pos: dict[str, float] = {}            # token -> net shares REALLY held (G6)
    day_cash = 0.0
    read_pos = 0
    last_fills = 0.0
    halted = False

    def do_cancel(key, reason):
        r = resting.pop(key, None)
        if not r:
            return True
        strat, token, side = key
        if clob:
            try:
                clob.cancel(r["id"])                                   # G3 wrapped
            except Exception as e:
                log.warning("cancel %s failed (%s) - dropped from book state", r["id"][:12], e)
        else:
            log.info("[dry] cancel %s %s %s@%.2f (%s)", strat, side, token[:10], r["price"], reason)
        led.order(strat, token, side, r["price"], r["size"], r["id"], f"cancel:{reason}")
        return True

    def cancel_everything(reason):
        for key in list(resting):
            do_cancel(key, reason)
        if clob:
            try:
                clob.cancel_all()                                      # belt and braces
            except Exception as e:
                log.warning("cancel_all failed: %s", e)

    try:
        while True:
            if os.path.exists(KILL):                                   # G10
                log.warning("KILL file present -> cancel everything + exit")
                cancel_everything("kill")
                return
            # ---- ingest intents (G11: rotation/truncation safe) ----
            try:
                if os.path.exists(INTENTS) and os.path.getsize(INTENTS) < read_pos:
                    log.warning("intents file shrank (rotation?) - resetting read position")
                    read_pos = 0
                with open(INTENTS, encoding="utf-8") as f:
                    f.seek(read_pos)
                    while True:
                        line = f.readline()        # NOT `for line in f`: iterator disables tell()
                        if not line:
                            break
                        read_pos = f.tell()
                        try:
                            it = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if it.get("strategy") in enabled and it.get("token"):
                            desired[(it["strategy"], it["token"])] = it
            except OSError:
                pass

            now = time.time()
            actions = 0                                                # G9
            for (strat, token), it in list(desired.items()):
                close_ts = slug_close_ts(it.get("slug", ""))
                expired = (close_ts is not None and now >= close_ts - CLOSE_EARLY_S)  # G4
                stale = now - float(it.get("ts", 0)) > STALE_INTENT_S
                if expired or stale:
                    for side in ("buy", "sell"):
                        do_cancel((strat, token, side), "window-close" if expired else "stale-intent")
                    del desired[(strat, token)]
                    continue
                for side, px_key, sh_key in (("buy", "bid", "bid_shares"), ("sell", "ask", "ask_shares")):
                    if actions >= ACTIONS_PER_LOOP:
                        break
                    want_px = it.get(px_key)
                    want_sh = float(it.get(sh_key) or 0) or MIN_SHARES
                    if want_px is not None:
                        want_px = round(round(want_px / TICK) * TICK, 2)
                        if not (TICK <= want_px <= 1.0 - TICK):
                            want_px = None
                    if want_px is not None:                            # G5 strict $ cap
                        max_sh = cap_ord / want_px
                        if max_sh < MIN_SHARES:
                            want_px = None
                        else:
                            want_sh = max(MIN_SHARES, min(want_sh, max_sh))
                    if want_px is not None and side == "buy":          # G7 global exposure cap
                        if max(0.0, -day_cash) >= cap_inv_total:
                            want_px = None
                    if want_px is not None and side == "sell" and clob:  # G6 holdings only
                        held = pos.get(token, 0.0)
                        if held < MIN_SHARES:
                            want_px = None
                        else:
                            want_sh = min(want_sh, held)
                    key = (strat, token, side)
                    have = resting.get(key)
                    if want_px is None:
                        if have:
                            do_cancel(key, "quote-off")
                            actions += 1
                        continue
                    if have and abs(have["price"] - want_px) < TICK / 2:
                        continue                                       # sub-tick: leave it alone
                    if have:
                        do_cancel(key, "reprice")
                        actions += 1
                    try:                                               # G3 wrapped placement
                        oid = clob.place(token, side, want_px, want_sh) if clob else f"dry-{int(now*1000)}-{actions}"
                        if not clob:
                            log.info("[dry] place %s %s %.1f sh %s @ %.2f", strat, side, want_sh, token[:10], want_px)
                        if oid:
                            resting[key] = {"id": oid, "price": want_px, "size": want_sh}
                            led.order(strat, token, side, want_px, want_sh, oid, "place")
                    except Exception as e:
                        log.warning("place %s %s@%.2f failed: %s", side, token[:10], want_px, e)
                    actions += 1

            # ---- fills -> positions, realized-basis day stop (place mode) ----
            if clob and now - last_fills > FILLS_EVERY_S:
                last_fills = now
                try:
                    for t in clob.trades():
                        try:
                            side = clob.my_side(t)
                            price, size = float(t["price"]), float(t["size"])
                            led.fill((float(t.get("match_time") or now), "live",
                                      t.get("asset_id", ""), side, price, size,
                                      price * size, str(t.get("id"))))
                        except (KeyError, TypeError, ValueError):
                            continue
                except Exception as e:
                    log.warning("trades pull failed: %s", e)
                day0 = now // 86400 * 86400
                pos, day_cash = led.positions_and_cash(day0)
                if day_cash <= -(cap_inv_total + stop):                # G8 realized basis
                    log.error("DAY LOSS STOP: net cash %+.2f beyond open-cap %+.2f + stop %.2f "
                              "-> cancel all + HALT for the day", day_cash, -cap_inv_total, stop)
                    cancel_everything("day-stop")
                    halted = True
                    return
            time.sleep(1.0)
    finally:
        if not halted:
            cancel_everything("shutdown")                              # G2: never leave orders behind
        log.info("executor exit - book is clean")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped by user.")
