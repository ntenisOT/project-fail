"""Lockbot: in-process set-arb taker for Polymarket 5m up/down markets.

Subscribes DIRECTLY to the market websocket (no file hop, no 1Hz throttle),
maintains best asks in memory (~6ms event cadence), and when
    ask_up + ask_dn < 1 - taker_fees - margin
fires BOTH legs as FAK concurrently (~28ms flight, probe-measured). A filled
set pays $1 at settlement regardless of outcome; the risk is ONLY legging
(one leg fills, the other misses) - bounded by the unwind.

Run it YOURSELF:

    LOCKBOT_MODE=shadow  python -m live.lockbot    # detection soak, NO orders,
                                                   # no credentials needed
    LOCKBOT_MODE=place   python -m live.lockbot    # REAL ORDERS. Needs
                                                   # POLY_PRIVATE_KEY (+funder)
                                                   # AND DEPLOY_REGION=eu-west-1

Guards (mirrors the executor's hardening):
  L1 place-mode geo interlock (DEPLOY_REGION=eu-west-1)
  L2 KILL file (paper/KILL) checked before every action -> exit
  L3 $ per-leg clip cap (default $5) and >=5 share exchange minimum
  L4 per-window spend cap (default $15) and per-day spend cap (default $60)
  L5 legged-unwind: single filled leg is FAK-sold back immediately at -2 ticks
  L6 3 legged events in a day -> self-halt (fill model is wrong, stop probing)
  L7 stale-book guard: both asks must be <2s fresh
  L8 same-state cooldown: one attempt per distinct book state per 2s
  L9 fill parsing is defensive: unknown response shape counts as FILLED for
     risk purposes (never under-counts exposure)
All detections/attempts land in live/lockbot.db (table locks) for the report.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import time
import urllib.request

from paper import envload

envload.load()
logging.basicConfig(level=logging.INFO, format="%(asctime)s lockbot %(message)s")
log = logging.getLogger("lockbot")

MODE = os.environ.get("LOCKBOT_MODE", "shadow")
ASSETS = {"btc": "btc-updown-5m", "eth": "eth-updown-5m",
          "sol": "sol-updown-5m", "xrp": "xrp-updown-5m"}
MKT_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
KILL = "paper/KILL"
DB = "live/lockbot.db"
TAKER_RATE = 0.07
MARGIN = float(os.environ.get("LOCKBOT_MARGIN", "0.005"))
CAP_LEG = float(os.environ.get("LOCKBOT_CAP_LEG", "5.0"))       # $ per leg
CAP_WINDOW = float(os.environ.get("LOCKBOT_CAP_WINDOW", "15.0"))  # $ per window
CAP_DAY = float(os.environ.get("LOCKBOT_CAP_DAY", "60.0"))        # $ per day
MIN_SHARES = 5.0
FRESH_S = 2.0
COOLDOWN_S = 2.0
MAX_LEGGED_PER_DAY = 3


def gamma_tokens(prefix: str, base: int):
    slug = f"{prefix}-{base}"
    try:
        req = urllib.request.Request(
            f"https://gamma-api.polymarket.com/events?slug={slug}",
            headers={"User-Agent": "Mozilla/5.0"})
        ev = json.load(urllib.request.urlopen(req, timeout=8))
        m = (ev[0].get("markets") or [{}])[0]
        ids = m.get("clobTokenIds")
        ids = json.loads(ids) if isinstance(ids, str) else ids
        return (slug, ids[0], ids[1]) if ids and len(ids) >= 2 else None
    except Exception as e:
        log.warning("gamma %s: %s", slug, e)
        return None


class Book:
    __slots__ = ("ask", "ask_sz", "ts")

    def __init__(self):
        self.ask = None
        self.ask_sz = 0.0
        self.ts = 0.0


class Lockbot:
    def __init__(self):
        self.clob = None
        if MODE == "place":
            if os.environ.get("DEPLOY_REGION") != "eu-west-1":       # L1
                raise SystemExit("L1 geo interlock: place mode only on eu-west-1")
            from live.executor import Clob
            self.clob = Clob()
        self.db = sqlite3.connect(DB)
        self.db.execute("""CREATE TABLE IF NOT EXISTS locks(
            ts REAL, asset TEXT, slug TEXT, up_px REAL, dn_px REAL, sh REAL,
            net_per_sh REAL, exp_usd REAL, mode TEXT, result TEXT)""")
        self.db.commit()
        self.tokens: dict[str, tuple] = {}      # asset -> (slug, up_tok, dn_tok, close_ts)
        self.books: dict[str, Book] = {}        # token -> Book
        self.tok_asset: dict[str, tuple] = {}   # token -> (asset, is_up)
        self.resub = asyncio.Event()
        self.window_spend: dict[str, float] = {}
        self.day_spend = 0.0
        self.day_key = time.strftime("%Y-%m-%d", time.gmtime())
        self.legged_today = 0
        self.cooldown: dict[str, tuple] = {}    # asset -> (state, ts)

    def rec(self, asset, slug, up_px, dn_px, sh, net, result):
        try:
            self.db.execute("INSERT INTO locks VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (time.time(), asset, slug, up_px, dn_px, sh, net,
                             sh * (up_px + dn_px), MODE, result))
            self.db.commit()
        except sqlite3.Error as e:
            log.warning("db: %s", e)

    async def roll_task(self):
        while True:
            base = int(time.time()) // 300 * 300
            changed = False
            for a, prefix in ASSETS.items():
                cur = self.tokens.get(a)
                if cur and cur[3] == base + 300:
                    continue
                r = await asyncio.to_thread(gamma_tokens, prefix, base)
                if r:
                    slug, up, dn = r
                    old = self.tokens.get(a)
                    if old:
                        for t in (old[1], old[2]):
                            self.books.pop(t, None)
                            self.tok_asset.pop(t, None)
                    self.tokens[a] = (slug, up, dn, base + 300)
                    self.tok_asset[up] = (a, True)
                    self.tok_asset[dn] = (a, False)
                    changed = True
            if changed:
                self.resub.set()
            await asyncio.sleep(5)

    def on_book(self, tok, asks):
        b = self.books.setdefault(tok, Book())
        if asks:
            best = min(asks, key=lambda x: float(x["price"]))
            b.ask = float(best["price"])
            b.ask_sz = float(best.get("size") or 0)
        else:
            b.ask, b.ask_sz = None, 0.0
        b.ts = time.time()
        info = self.tok_asset.get(tok)
        if info:
            self.check(info[0])

    def check(self, asset):
        if os.path.exists(KILL):                                     # L2
            raise SystemExit("KILL file -> lockbot exit")
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self.day_key:
            self.day_key, self.day_spend, self.legged_today = day, 0.0, 0
        if self.legged_today >= MAX_LEGGED_PER_DAY:                  # L6
            return
        t = self.tokens.get(asset)
        if not t:
            return
        slug, up, dn, close_ts = t
        now = time.time()
        if now > close_ts - 5:                  # never open a set in the last 5s
            return
        bu, bd = self.books.get(up), self.books.get(dn)
        if (not bu or not bd or bu.ask is None or bd.ask is None
                or now - bu.ts > FRESH_S or now - bd.ts > FRESH_S):  # L7
            return
        au, ad = bu.ask, bd.ask
        fee = TAKER_RATE * (au * (1 - au) + ad * (1 - ad))
        net = 1.0 - (au + ad) - fee
        if net <= MARGIN:
            return
        state = (au, ad)
        cd = self.cooldown.get(asset)
        if cd and cd[0] == state and now - cd[1] < COOLDOWN_S:       # L8
            return
        self.cooldown[asset] = (state, now)
        # L3 + depth-aware: never take more than BOTH displayed asks show -
        # FAK partial fills (the legging cause) come from overrunning the book.
        # WHOLE shares only: int shares x 2-decimal px = 2-decimal $ amount
        # (marketable orders reject >2-decimal amounts), and each leg needs
        # >= $1.00 notional (exchange minimum for marketable BUYs).
        sh = float(int(min(CAP_LEG / au, CAP_LEG / ad, bu.ask_sz, bd.ask_sz)))
        need = max(MIN_SHARES, math.ceil(1.0 / au), math.ceil(1.0 / ad))
        if sh < need:
            return
        spend = sh * (au + ad)
        if self.window_spend.get(slug, 0.0) + spend > CAP_WINDOW:    # L4
            return
        if self.day_spend + spend > CAP_DAY:
            return
        self.window_spend[slug] = self.window_spend.get(slug, 0.0) + spend
        self.day_spend += spend
        log.info("LOCK %s %s: %.2f+%.2f=%0.2f net %.4f/sh -> %.1f sh ($%.2f)%s",
                 asset, slug, au, ad, au + ad, net, sh, spend,
                 "" if MODE == "place" else " [shadow]")
        if MODE != "place":
            self.rec(asset, slug, au, ad, sh, net, "shadow-detect")
            return
        asyncio.get_event_loop().create_task(self.fire(asset, slug, up, dn, au, ad, sh, net))

    async def fire(self, asset, slug, up, dn, au, ad, sh, net):
        def leg(tok, px):
            return self.clob.place(tok, "buy", px, sh, post_only=False, fak=True)
        r_up, r_dn = await asyncio.gather(
            asyncio.to_thread(leg, up, au), asyncio.to_thread(leg, dn, ad),
            return_exceptions=True)

        def filled(r):
            if isinstance(r, Exception) or r is None:
                return 0.0
            if isinstance(r, dict):
                for k in ("makingAmount", "takingAmount", "sizeMatched", "matched"):
                    v = r.get(k)
                    if v is not None:
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
            return sh                                               # L9 assume filled
        f_up, f_dn = filled(r_up), filled(r_dn)
        matched = min(f_up, f_dn)
        naked_tok, naked_sh, naked_px = (up, f_up - matched, au) if f_up > f_dn else (dn, f_dn - matched, ad)
        if matched >= MIN_SHARES and naked_sh < MIN_SHARES:
            self.rec(asset, slug, au, ad, matched, net, "locked")
            log.info("LOCKED %s %.1f sh, net $%.2f", asset, matched, matched * net)
            return
        if naked_sh >= 0.1:                                          # L5 unwind
            self.legged_today += 1
            px = round(max(0.01, naked_px - 0.02), 2)
            naked_sh = int(naked_sh)                # whole shares (amount accuracy)
            if naked_sh < 1 or naked_sh * px < 1.0:  # below exchange minimums:
                self.rec(asset, slug, au, ad, naked_sh, net, "legged-small-hold")
                log.warning("LEGGED %s: %s sh too small to unwind - held to settle (max $1)",
                            asset, naked_sh)
                return
            try:
                await asyncio.to_thread(self.clob.place, naked_tok, "sell", px,
                                        naked_sh, False, True)
                self.rec(asset, slug, au, ad, naked_sh, net, "legged-unwound")
                log.warning("LEGGED %s: unwound %.1f sh @ %.2f (event %d/%d)",
                            asset, naked_sh, px, self.legged_today, MAX_LEGGED_PER_DAY)
            except Exception as e:
                self.rec(asset, slug, au, ad, naked_sh, net, "legged-STUCK")
                log.error("LEGGED %s: unwind FAILED (%s) - shares stuck, will settle", asset, e)
        else:
            self.rec(asset, slug, au, ad, 0.0, net, "missed-both")

    async def ws_task(self):
        while True:
            while not self.tok_asset:
                await asyncio.sleep(1)
            self.resub.clear()
            toks = list(self.tok_asset)
            try:
                import websockets
                async with websockets.connect(MKT_WS, ping_interval=None,
                                              open_timeout=12) as ws:
                    await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                    log.info("ws subscribed %d tokens (mode=%s margin=%.3f)",
                             len(toks), MODE, MARGIN)
                    while not self.resub.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        items = json.loads(raw)
                        for it in items if isinstance(items, list) else [items]:
                            if it.get("event_type") == "book":
                                self.on_book(it.get("asset_id"), it.get("asks") or [])
            except SystemExit:
                raise
            except Exception as e:
                log.warning("ws reconnect: %s", type(e).__name__)
                await asyncio.sleep(1)

    async def main(self):
        log.info("lockbot starting: mode=%s cap_leg=$%.0f cap_window=$%.0f cap_day=$%.0f",
                 MODE, CAP_LEG, CAP_WINDOW, CAP_DAY)
        await asyncio.gather(self.roll_task(), self.ws_task())


if __name__ == "__main__":
    if MODE not in ("shadow", "place"):
        raise SystemExit(f"LOCKBOT_MODE must be shadow|place, got {MODE}")
    try:
        asyncio.run(Lockbot().main())
    except SystemExit as e:
        log.warning("%s", e)
