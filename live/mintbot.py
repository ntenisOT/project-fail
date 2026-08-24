"""Mintbot v2: the winners' mechanic, rebuilt after the 6-agent review.
Per 5m window: splitPosition mints N sets at $1.00 flat, maker asks on BOTH
sides track fair with a JOINT-SUM FLOOR (never sell a $1 set below $1+margin),
matched leftovers MERGE back to $1 before close. Maker fee: zero.

Run it YOURSELF:

    MINTBOT_MODE=shadow  python -m live.mintbot   # no chain tx, no orders
    MINTBOT_MODE=place   python -m live.mintbot   # REAL. Needs MINTER_* in
                                                  # .env, DEPLOY_REGION=eu-west-1,
                                                  # and live/minter_approve run once

Guards (review-hardened):
  M1  geo interlock (place only on eu-west-1)
  M2  KILL file -> cancel ALL asks + exit; startup sweep cancels orphans;
      a finally-block cancels asks on ANY exit path
  M3  mint caps: $/window (MINT_USD) and $/day - counted pessimistically
      (BroadcastUncertain counts as minted; positions reconcile the truth)
  M4  mint only in the first 60s (fresh clock per attempt); never near close
  M5  close at T-20s: cancel asks (2 attempts), re-poll positions, merge
      int(min(held)); balance-revert -> auto-redeem path, NOT a chain failure
  M6  3 INFRA merge failures -> stop minting entirely (no virtual fallback)
  M7  preflight: exchange approval AND USDC.e allowance >= 8x MINT_USD AND
      balance >= 4x MINT_USD
  M8  quote sanity: both books fresh <3s, px in [0.05,0.95], joint sum floor
      px_up+px_dn >= 1.005
  M9  positions feed: a token ABSENT from the snapshot is UNKNOWN (not zero);
      closing windows are never fill-updated
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
MINT_USD = float(os.environ.get("MINT_USD", "20"))
MINT_DAY_CAP = float(os.environ.get("MINT_DAY_CAP", "250"))
SPREAD = float(os.environ.get("MINT_SPREAD", "0.02"))
SUM_FLOOR = 1.005            # M8: two asks must sum above set cost + margin
FRESH_S = 3.0
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
    __slots__ = ("ask", "ts")

    def __init__(self):
        self.ask = None
        self.ts = 0.0


class Mintbot:
    def __init__(self):
        self.clob = None
        self.key = os.environ.get("MINTER_PRIVATE_KEY")
        self.addr = os.environ.get("MINTER_ADDRESS")
        if MODE == "place":
            if os.environ.get("DEPLOY_REGION") != "eu-west-1":       # M1
                raise SystemExit("M1 geo interlock: place mode only on eu-west-1")
            if not self.key or not self.addr:
                raise SystemExit("MINTER_PRIVATE_KEY/MINTER_ADDRESS missing")
            from live import chain
            ap = chain.call(chain.CTF, chain.encode_call(
                "isApprovedForAll(address,address)", [self.addr, chain.CTF_EXCHANGE]))
            allowance = int(chain.call(chain.USDC_E, chain.encode_call(
                "allowance(address,address)", [self.addr, chain.CTF])), 16) / 1e6
            balance = chain.erc20_balance(chain.USDC_E, self.addr)
            if int(ap, 16) != 1:                                     # M7
                raise SystemExit("M7: run live/minter_approve first (exchange approval)")
            if allowance < 8 * MINT_USD:
                raise SystemExit(f"M7: USDC.e allowance ${allowance:.0f} < 8x MINT_USD - "
                                 "run live/minter_approve (it grants a large allowance)")
            if balance < 4 * MINT_USD:
                raise SystemExit(f"M7: USDC.e balance ${balance:.2f} < 4x MINT_USD")
            from live.executor import Clob
            self.clob = Clob(key=self.key, funder=None, sig=0)
            try:
                self.clob.cancel_all()                               # M2 startup sweep
                log.info("startup cancel_all: book swept clean")
            except Exception as e:
                log.warning("startup cancel_all: %s", e)
        self.db = sqlite3.connect(DB)
        self.db.execute("""CREATE TABLE IF NOT EXISTS mint_windows(
            ts REAL, asset TEXT, slug TEXT, minted REAL, sold_up REAL, sold_dn REAL,
            sold_usd REAL, merged REAL, mode TEXT, note TEXT)""")
        self.db.commit()
        self.state: dict[str, dict] = {}
        self.books: dict[str, Book] = {}
        self.tok_asset: dict[str, tuple] = {}
        self.resub = asyncio.Event()
        self.day_minted = 0.0
        self.day_key = time.strftime("%Y-%m-%d", time.gmtime())
        self.infra_merge_fails = 0            # M6: infra only, not balance reverts
        self.pos: dict[str, dict] = {}
        self.pos_fresh = 0.0
        self.tasks: set = set()               # hard refs: tasks must never be GC'd
        self.opening: set = set()             # assets with an open_window in flight

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

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
        while True:
            if os.path.exists(KILL):
                raise SystemExit("M2 KILL -> mintbot exit")           # finally cancels asks
            day = time.strftime("%Y-%m-%d", time.gmtime())
            if day != self.day_key:
                self.day_key, self.day_minted = day, 0.0
            base = int(time.time()) // 300 * 300
            for a in ASSETS:
                st = self.state.get(a)
                if st and st["close"] - 20 <= time.time() and not st.get("closing"):
                    st["closing"] = True
                    self.spawn(self.close_window(st))
                if (not st or st["base"] != base) and a not in self.opening:
                    self.opening.add(a)
                    self.spawn(self.open_window(a, base))
            await asyncio.sleep(2)

    async def open_window(self, asset, base):
        from live import chain
        try:
            if time.time() - base > 60:                              # M4
                return
            if MODE == "place" and self.infra_merge_fails >= 3:      # M6: full stop
                return
            if self.day_minted + MINT_USD > MINT_DAY_CAP:            # M3
                log.warning("M3 day mint cap - skipping %s %s", asset, base)
                return
            r = await asyncio.to_thread(gamma_market, ASSETS[asset], base)
            if not r:
                return
            slug, cond, up, dn = r
            if time.time() - base > 60:                              # M4 fresh clock
                return
            st = {"asset": asset, "slug": slug, "base": base, "close": base + 300,
                  "cond": cond, "up": up, "dn": dn, "minted": 0.0,
                  "sold": {True: 0.0, False: 0.0}, "sold_usd": 0.0,
                  "asks": {True: None, False: None}, "last_q": {True: 0.0, False: 0.0}}
            if MODE == "place":
                self.day_minted += MINT_USD                          # M3 pessimistic
                try:
                    await asyncio.to_thread(chain.split, self.key, cond, MINT_USD)
                    st["minted"] = MINT_USD
                    log.info("MINTED $%.0f -> %s", MINT_USD, slug)
                except chain.PreflightError as e:
                    self.day_minted -= MINT_USD                      # $0 moved, refund cap
                    log.error("mint preflight %s: %s", slug, e)
                    return
                except chain.BroadcastUncertain as e:
                    st["minted"] = MINT_USD                          # assume minted;
                    log.warning("mint UNCERTAIN %s: %s - positions will reconcile", slug, e)
                except Exception as e:
                    log.error("mint failed %s: %s", slug, e)
                    return
            else:
                st["minted"] = MINT_USD
                log.info("[shadow] MINT $%.0f -> %s", MINT_USD, slug)
            old = self.state.get(asset)
            if old:                                        # prune dead tokens
                for t in (old["up"], old["dn"]):
                    self.tok_asset.pop(t, None)
                    self.books.pop(t, None)
            self.tok_asset[up] = (asset, True)
            self.tok_asset[dn] = (asset, False)
            self.state[asset] = st
            self.resub.set()
            if self.clob:                                  # pre-warm order path
                for tok in (up, dn):
                    try:
                        await asyncio.to_thread(self.clob.c.get_tick_size, tok)
                        await asyncio.to_thread(self.clob.c.get_neg_risk, tok)
                    except Exception:
                        pass
        finally:
            self.opening.discard(asset)

    async def close_window(self, st):
        from live import chain
        if MODE == "place" and self.clob:
            for side in (True, False):                     # M5: cancel, 2 attempts
                ask = st["asks"].get(side)
                if ask and ask[1]:
                    for _ in range(2):
                        try:
                            await asyncio.to_thread(self.clob.cancel, ask[1])
                            break
                        except Exception:
                            await asyncio.sleep(1)
                st["asks"][side] = None
            await asyncio.sleep(8)                         # let last fills index
            try:
                self.pos = await asyncio.to_thread(self.clob.positions)
                self.pos_fresh = time.time()
            except Exception:
                pass
            held_up = self.pos.get(st["up"], {}).get("sh")
            held_dn = self.pos.get(st["dn"], {}).get("sh")
            if held_up is None or held_dn is None:
                log.warning("close %s: positions not indexed - leaving to auto-redeem",
                            st["slug"])
            else:
                matched = float(int(max(0.0, min(held_up, held_dn))))
                if matched >= 1.0:
                    try:
                        await asyncio.to_thread(chain.merge, self.key, st["cond"], matched)
                        st["merged"] = matched
                        self.infra_merge_fails = 0
                    except RuntimeError as e:
                        if "REVERTED" in str(e) or isinstance(e, chain.PreflightError):
                            # stale size / balance mismatch: auto-redeem covers it -
                            # NOT an infra failure (M5)
                            log.warning("merge skipped %s (%s) - auto-redeem path", st["slug"], e)
                        else:
                            self.infra_merge_fails += 1              # M6
                            log.error("merge INFRA fail %s (#%d): %s",
                                      st["slug"], self.infra_merge_fails, e)
        else:
            left_up = st["minted"] - st["sold"][True]
            left_dn = st["minted"] - st["sold"][False]
            st["merged"] = max(0.0, min(left_up, left_dn))
        pnl_sold = st["sold_usd"] - (st["sold"][True] + st["sold"][False]) * 0.5
        log.info("%sCLOSE %s: sold %.0fU/%.0fD ($%.2f) merged %.0f est_pnl %+.2f",
                 "" if MODE == "place" else "[shadow] ", st["slug"], st["sold"][True],
                 st["sold"][False], st["sold_usd"], st.get("merged", 0), pnl_sold)
        self.rec(st, "close")

    # ---- fill truth (M9): absent token = UNKNOWN, closing windows untouched
    async def positions_task(self):
        while True:
            await asyncio.sleep(10)
            if not self.clob or not self.state:
                continue
            try:
                self.pos = await asyncio.to_thread(self.clob.positions)
                self.pos_fresh = time.time()
            except Exception as e:
                log.warning("positions poll: %s", e)
                continue
            for st in list(self.state.values()):
                if st.get("closing") or st["minted"] <= 0:
                    continue
                for side, tok in ((True, st["up"]), (False, st["dn"])):
                    p = self.pos.get(tok)
                    if p is None or p.get("sh") is None:
                        continue                           # M9: unknown, not zero
                    sold = max(0.0, st["minted"] - p["sh"])
                    if sold > st["sold"][side] + 0.01:
                        delta = sold - st["sold"][side]
                        ask = st["asks"].get(side)
                        px = ask[0] if ask else 0.5
                        st["sold_usd"] += delta * px
                        log.info("FILLED %s %s +%.0f sh (~$%.2f)", st["asset"],
                                 "Up" if side else "Dn", delta, delta * px)
                        st["sold"][side] = sold
                    elif sold < st["sold"][side] - 0.01:
                        st["sold"][side] = sold            # snap-back, no $ booked

    # ---- quoting: async task, order I/O off the loop, joint-sum floor ------
    def on_book(self, tok, asks):
        b = self.books.setdefault(tok, Book())
        b.ask = min((float(x["price"]) for x in asks), default=None) if asks else None
        b.ts = time.time()

    async def quoter_task(self):
        while True:
            await asyncio.sleep(REQUOTE_S)
            now = time.time()
            for st in list(self.state.values()):
                if (st.get("closing") or st["minted"] <= 0
                        or now < st["base"] + 3 or now > st["close"] - 25):
                    continue
                bu, bd = self.books.get(st["up"]), self.books.get(st["dn"])
                if (not bu or not bd or bu.ask is None or bd.ask is None
                        or now - bu.ts > FRESH_S or now - bd.ts > FRESH_S):   # M8
                    continue
                fair_u, fair_d = 1.0 - bd.ask, 1.0 - bu.ask
                px_u = max(0.05, min(0.95, round(fair_u + SPREAD, 2)))
                px_d = max(0.05, min(0.95, round(fair_d + SPREAD, 2)))
                if px_u + px_d < SUM_FLOOR:                # M8 joint floor
                    bump = (SUM_FLOOR - px_u - px_d) / 2
                    px_u = min(0.95, round(px_u + bump + 0.005, 2))
                    px_d = min(0.95, round(px_d + bump + 0.005, 2))
                for side, px in ((True, px_u), (False, px_d)):
                    await self.requote(st, side, px)

    async def requote(self, st, side, px):
        held = st["minted"] - st["sold"][side]
        sh = float(int(min(held, MINT_USD)))
        if sh < MIN_SHARES or px * sh < 1.0:
            return
        cur = st["asks"].get(side)
        if cur and cur[0] == px and cur[1] is not None:
            return
        tok = st["up"] if side else st["dn"]
        if MODE == "place" and self.clob:
            if cur and cur[1]:
                try:
                    await asyncio.to_thread(self.clob.cancel, cur[1])
                except Exception:
                    pass
            st["asks"][side] = None                        # truth until proven resting
            try:
                oid = await asyncio.to_thread(
                    self.clob.place, tok, "sell", px, sh, True)
                if oid:
                    st["asks"][side] = (px, oid)
            except Exception as e:
                if "post-only" not in str(e):
                    log.warning("quote %s %s@%.2f: %s", st["asset"],
                                "U" if side else "D", px, e)
        else:
            if not cur or cur[0] != px:
                st["asks"][side] = (px, "shadow")
                log.info("[shadow] ASK %s %s %.0f sh @ %.2f", st["asset"],
                         "Up" if side else "Dn", sh, px)

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
                    log.info("ws subscribed %d tokens (mode=%s mint=$%.0f spread=%.2f)",
                             len(toks), MODE, MINT_USD, SPREAD)
                    while not self.resub.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue                       # re-check resub fast
                        items = json.loads(raw)
                        for it in items if isinstance(items, list) else [items]:
                            if it.get("event_type") == "book":
                                self.on_book(it.get("asset_id"), it.get("asks") or [])
            except SystemExit:
                raise
            except Exception as e:
                log.warning("ws reconnect: %s", type(e).__name__)
                await asyncio.sleep(1)

    async def cancel_everything(self):
        if not (MODE == "place" and self.clob):
            return
        for st in self.state.values():
            for side in (True, False):
                ask = st["asks"].get(side)
                if ask and ask[1]:
                    try:
                        await asyncio.to_thread(self.clob.cancel, ask[1])
                    except Exception:
                        pass
        try:
            await asyncio.to_thread(self.clob.cancel_all)
        except Exception as e:
            log.warning("exit cancel_all: %s", e)
        log.info("exit: all asks cancelled (minted sets merge/redeem on their own)")

    async def main(self):
        log.info("mintbot v2 starting: mode=%s mint=$%.0f/window day-cap=$%.0f "
                 "spread=%.2f sum-floor=%.3f", MODE, MINT_USD, MINT_DAY_CAP,
                 SPREAD, SUM_FLOOR)
        try:
            await asyncio.gather(self.roll_task(), self.ws_task(),
                                 self.positions_task(), self.quoter_task())
        finally:
            await self.cancel_everything()                 # M2: every exit path


if __name__ == "__main__":
    if MODE not in ("shadow", "place"):
        raise SystemExit(f"MINTBOT_MODE must be shadow|place, got {MODE}")
    try:
        asyncio.run(Mintbot().main())
    except (SystemExit, KeyboardInterrupt) as e:
        log.warning("%s", e)
