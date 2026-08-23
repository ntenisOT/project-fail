"""Live executor: consumes paper/intents.jsonl (desired quotes from the paper
runner's LiveGate) and maintains real resting limit orders on the Polymarket
CLOB. Run it YOURSELF:

    python -m live.executor

Modes (LIVE_EXECUTOR_MODE in .env):
  log-only : full reconcile loop, prints every order it WOULD place/cancel.
             No credentials needed. Prove the loop like this first.
  place    : REAL ORDERS. Needs POLY_PRIVATE_KEY (+ POLY_FUNDER for site
             accounts) in .env. You type the launch command; nothing here
             starts trading by itself.

Safety, rechecked here (last gate after LiveGate's first gate):
  * only strategies enabled in paper/live.json; caps re-enforced
  * paper/KILL         -> cancel everything, stop placing, exit
  * daily loss stop    -> cancel + stop for the UTC day
  * per-order notional cap, per-strategy inventory cap
Fills are pulled from the CLOB trade feed into live/live.db so the Telegram
live report shows REAL profitability, not sim numbers.
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
HOST = "https://clob.polymarket.com"


def cfg() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


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
        self.db.execute("INSERT INTO live_orders VALUES (?,?,?,?,?,?,?,?)",
                        (time.time(), strategy, token, side, price, size, oid, action))
        self.db.commit()

    def fill(self, rec):
        try:
            self.db.execute("INSERT OR IGNORE INTO live_fills VALUES (?,?,?,?,?,?,?,?)", rec)
            self.db.commit()
        except sqlite3.Error as e:
            log.warning("fill write: %s", e)


class Clob:
    """Thin wrapper; only constructed in place mode."""
    def __init__(self):
        from py_clob_client.client import ClobClient          # import only when needed
        key = os.environ["POLY_PRIVATE_KEY"]
        funder = os.environ.get("POLY_FUNDER") or None
        sig = int(os.environ.get("POLY_SIGNATURE_TYPE", "2"))
        kw = {"key": key, "chain_id": 137}
        if funder:
            kw.update({"signature_type": sig, "funder": funder})
        self.c = ClobClient(HOST, **kw)
        self.c.set_api_creds(self.c.create_or_derive_api_creds())
        self.addr = self.c.get_address()
        log.info("CLOB client ready (addr %s...%s)", self.addr[:6], self.addr[-4:])

    def place(self, token, side, price, size):
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        args = OrderArgs(price=round(price, 2), size=round(size, 1),
                         side=BUY if side == "buy" else SELL, token_id=token)
        r = self.c.post_order(self.c.create_order(args), OrderType.GTC)
        return (r or {}).get("orderID")

    def cancel(self, oid):
        try:
            self.c.cancel(oid)
        except Exception as e:
            log.warning("cancel %s: %s", oid[:10], e)

    def cancel_all(self):
        try:
            self.c.cancel_all()
        except Exception as e:
            log.warning("cancel_all: %s", e)

    def trades(self):
        try:
            from py_clob_client.clob_types import TradeParams
            return self.c.get_trades(TradeParams(maker_address=self.addr)) or []
        except Exception:
            return []


def main():
    conf = cfg()
    enabled = set(conf.get("enabled") or [])
    cap_ord = float(conf.get("max_order_usd", 5.0))
    cap_inv = float(conf.get("max_inventory_usd", 50.0))
    stop = float(conf.get("daily_loss_stop_usd", 25.0))
    log.info("mode=%s enabled=%s caps: order $%.0f, inventory $%.0f, day-stop $%.0f",
             MODE, sorted(enabled), cap_ord, cap_inv, stop)
    if MODE == "place":
        # geo interlock: UK box is paper-only (Polymarket blocks UK trading).
        # place-mode only runs where the operator has declared the permitted region.
        if os.environ.get("DEPLOY_REGION") != "eu-west-1":
            log.error("REFUSED: place mode requires DEPLOY_REGION=eu-west-1 in .env "
                      "(this box is paper-only; deploy to the Ireland box per GO_LIVE.md)")
            return
        clob = Clob()
    else:
        clob = None
        log.info("LOG-ONLY: no orders will be sent")

    led = Ledger()
    desired: dict[tuple, dict] = {}      # (strategy, token) -> latest intent
    resting: dict[tuple, dict] = {}      # (strategy, token, side) -> {id, price, size}
    inv_usd: dict[str, float] = {}       # strategy -> filled exposure (from live fills)
    pos = 0
    last_trades = 0.0

    while True:
        if os.path.exists(KILL):
            log.warning("KILL file present -> cancel all + exit")
            if clob:
                clob.cancel_all()
            return
        # ingest new intents
        try:
            with open(INTENTS, encoding="utf-8") as f:
                f.seek(pos)
                while True:
                    line = f.readline()          # NOT `for line in f`: iterator disables tell()
                    if not line:
                        break
                    pos = f.tell()
                    try:
                        it = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if it.get("strategy") in enabled:
                        desired[(it["strategy"], it["token"])] = it
        except OSError:
            pass
        # reconcile desired vs resting
        for (strat, token), it in list(desired.items()):
            age = time.time() - it["ts"]
            if age > 240:                          # stale window rolled: drop + cancel
                for side in ("buy", "sell"):
                    r = resting.pop((strat, token, side), None)
                    if r and clob:
                        clob.cancel(r["id"])
                del desired[(strat, token)]
                continue
            for side, px_key, sh_key in (("buy", "bid", "bid_shares"), ("sell", "ask", "ask_shares")):
                want_px = it.get(px_key)
                want_sh = float(it.get(sh_key) or 0)
                if want_px is not None:
                    want_px = round(round(want_px / TICK) * TICK, 2)   # tick-round BEFORE diffing: no churn on sub-tick moves
                    want_sh = max(MIN_SHARES, min(want_sh, cap_ord / max(want_px, TICK)))
                    if inv_usd.get(strat, 0.0) >= cap_inv and side == "buy":
                        want_px = None                    # exposure cap
                have = resting.get((strat, token, side))
                if want_px is None:
                    if have:
                        (clob.cancel(have["id"]) if clob else log.info("[dry] cancel %s %s %s@%.2f", strat, side, token[:10], have["price"]))
                        led.order(strat, token, side, have["price"], have["size"], have["id"], "cancel")
                        resting.pop((strat, token, side))
                    continue
                if have and abs(have["price"] - want_px) < TICK / 2:
                    continue                              # already resting at the right price
                if have:
                    (clob.cancel(have["id"]) if clob else log.info("[dry] cancel %s %s %s@%.2f", strat, side, token[:10], have["price"]))
                    led.order(strat, token, side, have["price"], have["size"], have["id"], "cancel")
                    resting.pop((strat, token, side))
                oid = clob.place(token, side, want_px, want_sh) if clob else f"dry-{int(time.time()*1000)}"
                if not clob:
                    log.info("[dry] place %s %s %.1f sh %s @ %.2f", strat, side, want_sh, token[:10], want_px)
                if oid:
                    resting[(strat, token, side)] = {"id": oid, "price": want_px, "size": want_sh}
                    led.order(strat, token, side, want_px, want_sh, oid, "place")
        # pull real fills every 30s (place mode)
        if clob and time.time() - last_trades > 30:
            last_trades = time.time()
            for t in clob.trades():
                try:
                    usd = float(t["price"]) * float(t["size"])
                    led.fill((float(t.get("match_time") or time.time()), "live", t.get("asset_id", ""),
                              t.get("side", ""), float(t["price"]), float(t["size"]), usd, t.get("id")))
                except (KeyError, TypeError, ValueError):
                    continue
            day0 = time.time() // 86400 * 86400
            spent = led.db.execute("""SELECT COALESCE(sum(CASE WHEN side='BUY' THEN -usd ELSE usd END),0)
                                      FROM live_fills WHERE ts>=?""", (day0,)).fetchone()[0]
            inv_usd["binance_only"] = inv_usd["deribit_only"] = max(0.0, -spent) / max(1, len(enabled))
            if spent <= -stop:
                log.error("DAY LOSS STOP hit (%.2f) -> cancel all + halt", spent)
                clob.cancel_all()
                return
        time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped.")
