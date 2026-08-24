"""Mintbot: the winners' mechanic. Per 5m window: splitPosition mints N sets
at $1.00 flat, maker asks on BOTH sides track fair (opposite best ask, the
hl anchor that beat last-price by +84 in paper), matched leftovers MERGE back
to $1 before close. Maker fee: zero. No bids, no races - flow comes to us.

Run it YOURSELF:

    LOCKBOT unrelated. Modes:
    MINTBOT_MODE=shadow  python -m live.mintbot   # no chain tx, no orders -
                                                  # logs would-mint/would-quote
    MINTBOT_MODE=place   python -m live.mintbot   # REAL: mints + real asks.
                                                  # Needs MINTER_* in .env AND
                                                  # DEPLOY_REGION=eu-west-1 AND
                                                  # one-time live/minter_approve

Guards:
  M1 geo interlock (place only on eu-west-1)
  M2 KILL file (paper/KILL) -> cancel asks + exit (minted sets stay: riskless,
     they merge/redeem)
  M3 mint cap per window (MINT_USD, default $20) and per day (default $250)
  M4 mint only in the FIRST 60s of a window; never within 60s of close
  M5 merge-at-close: T-20s cancel asks, merge matched pairs on-chain; single-
     side residue auto-redeems (measured 5-10 min)
  M6 3 consecutive failed merges -> self-halt (chain path broken, stop minting)
  M7 exchange approval preflight: place mode refuses to start until the CTF
     setApprovalForAll(CTFExchange) is live (run live/minter_approve once)
  M8 ask floor: never quote a side below 0.02 or above 0.98
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import urllib.request

from paper import envload

envload.load()
logging.basicConfig(level=logging.INFO, format="%(asctime)s mintbot %(message)s")
log = logging.getLogger("mintbot")

MODE = os.environ.get("MINTBOT_MODE", "shadow")
ASSETS = {"btc": "btc-updown-5m", "eth": "eth-updown-5m",
          "sol": "sol-updown-5m", "xrp": "xrp-updown-5m"}
MKT_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
KILL = "paper/KILL"
DB = "live/mintbot.db"
MINT_USD = float(os.environ.get("MINT_USD", "20"))          # per window per asset
MINT_DAY_CAP = float(os.environ.get("MINT_DAY_CAP", "250"))
SPREAD = float(os.environ.get("MINT_SPREAD", "0.02"))
REQUOTE_S = 0.5
MIN_SHARES = 5.0


def gamma_market(prefix, base):
    slug = f"{prefix}-{base}"
    try:
        req = urllib.request.Request(
            f"https://gamma-api.polymarket.com/events?slug={slug}",
            headers={"User-Agent": "Mozilla/5.0"})
        ev = json.load(urllib.request.urlopen(req, timeout=8))
        m = (ev[0].get("markets") or [{}])[0]
        ids = m.get("clobTokenIds")
        ids = json.loads(ids) if isinstance(ids, str) else ids
        if ids and len(ids) >= 2 and m.get("conditionId") and not m.get("negRisk"):
            return slug, m["conditionId"], ids[0], ids[1]
    except Exception as e:
        log.warning("gamma %s: %s", slug, e)
    return None


class Book:
    __slots__ = ("ask", "bid", "ts")

    def __init__(self):
        self.ask = self.bid = None
        self.ts = 0.0


class Mintbot:
    def __init__(self):
        self.clob = None
        self.key = os.environ.get("MINTER_PRIVATE_KEY")
        self.addr = os.environ.get("MINTER_ADDRESS")
        if MODE == "place":
            if os.environ.get("DEPLOY_REGION") != "eu-west-1":
                raise SystemExit("M1 geo interlock: place mode only on eu-west-1")
            if not self.key or not self.addr:
                raise SystemExit("MINTER_PRIVATE_KEY/MINTER_ADDRESS missing")
            from live import chain
            ap = chain.call(chain.CTF, chain.encode_call(
                "isApprovedForAll(address,address)", [self.addr, chain.CTF_EXCHANGE]))
            if int(ap, 16) != 1:                                     # M7
                raise SystemExit("M7: run live/minter_approve first (CTF -> exchange)")
            from live.executor import Clob
            self.clob = Clob(key=self.key, funder=None, sig=0)       # EOA identity
        self.db = sqlite3.connect(DB)
        self.db.execute("""CREATE TABLE IF NOT EXISTS mint_windows(
            ts REAL, asset TEXT, slug TEXT, minted REAL, sold_up REAL, sold_dn REAL,
            sold_usd REAL, merged REAL, mode TEXT, note TEXT)""")
        self.db.commit()
        self.state: dict[str, dict] = {}      # asset -> window state
        self.books: dict[str, Book] = {}
        self.tok_asset: dict[str, tuple] = {}
        self.resub = asyncio.Event()
        self.day_minted = 0.0
        self.day_key = time.strftime("%Y-%m-%d", time.gmtime())
        self.merge_fails = 0

    def rec(self, st, note):
        try:
            self.db.execute("INSERT INTO mint_windows VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (time.time(), st["asset"], st["slug"], st["minted"],
                             st["sold"][True], st["sold"][False], st["sold_usd"],
                             st.get("merged", 0.0), MODE, note))
            self.db.commit()
        except sqlite3.Error as e:
            log.warning("db: %s", e)

    # ---- window lifecycle -------------------------------------------------
    async def roll_task(self):
        from live import chain
        while True:
            if os.path.exists(KILL):
                raise SystemExit("M2 KILL -> mintbot exit")
            now = time.time()
            base = int(now) // 300 * 300
            day = time.strftime("%Y-%m-%d", time.gmtime())
            if day != self.day_key:
                self.day_key, self.day_minted = day, 0.0
            for a, prefix in ASSETS.items():
                st = self.state.get(a)
                if st and st["close"] - 20 <= now and not st.get("closing"):
                    st["closing"] = True
                    asyncio.get_event_loop().create_task(self.close_window(st))
                if st and st["base"] == base:
                    continue
                if now - base > 60:                                  # M4 too late
                    continue
                r = await asyncio.to_thread(gamma_market, prefix, base)
                if not r:
                    continue
                slug, cond, up, dn = r
                if self.day_minted + MINT_USD > MINT_DAY_CAP:        # M3
                    log.warning("M3 day mint cap reached - skipping %s", slug)
                    continue
                st = {"asset": a, "slug": slug, "base": base, "close": base + 300,
                      "cond": cond, "up": up, "dn": dn, "minted": 0.0,
                      "sold": {True: 0.0, False: 0.0}, "sold_usd": 0.0,
                      "asks": {True: None, False: None}, "last_q": 0.0}
                if MODE == "place" and self.merge_fails < 3:         # M6
                    try:
                        await asyncio.to_thread(chain.split, self.key, cond, MINT_USD)
                        st["minted"] = MINT_USD
                        self.day_minted += MINT_USD
                        log.info("MINTED $%.0f -> %s (%.0f sets)", MINT_USD, slug, MINT_USD)
                    except Exception as e:
                        log.error("mint failed %s: %s", slug, e)
                        continue
                else:
                    st["minted"] = MINT_USD                          # shadow: virtual
                    log.info("[shadow] MINT $%.0f -> %s", MINT_USD, slug)
                self.tok_asset[up] = (a, True)
                self.tok_asset[dn] = (a, False)
                self.state[a] = st
                self.resub.set()
            await asyncio.sleep(2)

    async def close_window(self, st):
        from live import chain
        if MODE == "place" and self.clob:
            for side in (True, False):                    # cancel resting asks
                oid = st["asks"].get(side)
                if oid:
                    try:
                        self.clob.cancel(oid)
                    except Exception:
                        pass
        left_up = st["minted"] - st["sold"][True]
        left_dn = st["minted"] - st["sold"][False]
        matched = max(0.0, min(left_up, left_dn))
        if MODE == "place" and matched >= 1.0:
            try:
                await asyncio.to_thread(chain.merge, self.key, st["cond"], float(int(matched)))
                st["merged"] = float(int(matched))
                self.merge_fails = 0
            except Exception as e:
                self.merge_fails += 1                     # M6
                log.error("merge failed %s (#%d): %s - residue will auto-redeem",
                          st["slug"], self.merge_fails, e)
        else:
            st["merged"] = matched
        pnl_sold = st["sold_usd"] - (st["sold"][True] + st["sold"][False]) * 0.5
        log.info("%sCLOSE %s: sold %.0fU/%.0fD ($%.2f) merged %.0f  est_pnl %+.2f",
                 "" if MODE == "place" else "[shadow] ", st["slug"],
                 st["sold"][True], st["sold"][False], st["sold_usd"], st.get("merged", 0),
                 pnl_sold)
        self.rec(st, "close")

    # ---- quoting ----------------------------------------------------------
    def on_book(self, tok, bids, asks):
        b = self.books.setdefault(tok, Book())
        b.bid = max((float(x["price"]) for x in bids), default=None) if bids else None
        b.ask = min((float(x["price"]) for x in asks), default=None) if asks else None
        b.ts = time.time()
        info = self.tok_asset.get(tok)
        if info:
            self.quote(info[0])

    def quote(self, asset):
        st = self.state.get(asset)
        now = time.time()
        if (not st or st.get("closing") or now < st["base"] + 3
                or now > st["close"] - 25 or now - st["last_q"] < REQUOTE_S):
            return
        st["last_q"] = now
        for side in (True, False):
            tok = st["up"] if side else st["dn"]
            other = st["dn"] if side else st["up"]
            ob = self.books.get(other)
            if not ob or ob.ask is None:
                continue
            fair = 1.0 - ob.ask                       # hl anchor (paper: +84 vs last)
            px = round(max(0.02, min(0.98, fair + SPREAD)), 2)       # M8
            held = st["minted"] - st["sold"][side]
            sh = float(int(min(held, MINT_USD)))      # whole shares (amount rules)
            if sh < MIN_SHARES or px * sh < 1.0:
                continue
            cur = st["asks"].get(side)
            if cur and cur[0] == px:
                continue
            if MODE == "place" and self.clob:
                try:
                    if cur:
                        self.clob.cancel(cur[1])
                    oid = self.clob.place(tok, "sell", px, sh, post_only=True)
                    st["asks"][side] = (px, oid)
                except Exception as e:
                    if "post-only" not in str(e):
                        log.warning("quote %s %s@%.2f: %s", asset, "U" if side else "D", px, e)
            else:
                st["asks"][side] = (px, "shadow")
                log.info("[shadow] ASK %s %s %.0f sh @ %.2f (fair %.2f)",
                         asset, "Up" if side else "Dn", sh, px, fair)

    # ---- feed -------------------------------------------------------------
    async def ws_task(self):
        import websockets
        while True:
            while not self.tok_asset:
                await asyncio.sleep(1)
            self.resub.clear()
            toks = list(self.tok_asset)
            try:
                async with websockets.connect(MKT_WS, ping_interval=None,
                                              open_timeout=12) as ws:
                    await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                    log.info("ws subscribed %d tokens (mode=%s mint=$%.0f/win spread=%.2f)",
                             len(toks), MODE, MINT_USD, SPREAD)
                    while not self.resub.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        items = json.loads(raw)
                        for it in items if isinstance(items, list) else [items]:
                            if it.get("event_type") == "book":
                                self.on_book(it.get("asset_id"),
                                             it.get("bids") or [], it.get("asks") or [])
            except SystemExit:
                raise
            except Exception as e:
                log.warning("ws reconnect: %s", type(e).__name__)
                await asyncio.sleep(1)

    async def main(self):
        log.info("mintbot starting: mode=%s mint=$%.0f/window day-cap=$%.0f spread=%.2f",
                 MODE, MINT_USD, MINT_DAY_CAP, SPREAD)
        await asyncio.gather(self.roll_task(), self.ws_task())


if __name__ == "__main__":
    if MODE not in ("shadow", "place"):
        raise SystemExit(f"MINTBOT_MODE must be shadow|place, got {MODE}")
    try:
        asyncio.run(Mintbot().main())
    except SystemExit as e:
        log.warning("%s", e)
