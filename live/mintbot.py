"""Experimental mint-inventory market maker; shadow-only until its edge is proven.
Per 5m window: splitPosition mints N sets at $1.00 flat, maker asks on BOTH
sides track fair with a quoted-pair sum floor, and matched leftovers merge
before close. The floor does not guarantee paired fills. Maker fee: zero.

Run the control-plane soak:

    MINTBOT_MODE=shadow  python -m live.mintbot   # no chain tx, no orders
    MINTBOT_MODE=place   python -m live.mintbot   # hard-refused until V2 adapter,
                                                  # fill reconciliation, and edge pass

Inactive place-path guards (place mode is additionally hard-disabled):
  M1  geo interlock (place only on eu-west-1)
  M2  KILL file -> cancel ALL asks + exit; startup sweep cancels orphans;
      a finally-block cancels asks on ANY exit path
  M3  mint caps: $/window (MINT_USD) and $/day - counted pessimistically
      (BroadcastUncertain counts as minted; positions reconcile the truth)
  M4  mint only in the first 60s (fresh clock per attempt); never near close
  M5  close at T-20s: cancel asks (2 attempts), re-poll positions, merge
      int(min(held)); balance-revert -> auto-redeem path, NOT a chain failure
  M6  3 INFRA merge failures -> stop minting entirely (no virtual fallback)
  M7  preflight: V2 exchange approval AND pUSD allowance >= 8x MINT_USD AND
      balance >= 4x MINT_USD
  M8  quote sanity: delta-correct books plus market-channel heartbeat <12s,
      guarded prices, old pair
      verified gone before batch replacement, and quoted sum >= 1.005
  M9  positions feed: a token ABSENT from the snapshot is UNKNOWN (not zero);
      closing windows are never fill-updated
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import sqlite3
import time
import urllib.request

from live.market_book import BestAskCache
from live.feed_health import FeedHealth
from live.mint_quotes import Quote, guarded_pair_prices, plan_pair_quotes, should_reprice
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
FRESH_S = 12.0
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


class Mintbot:
    def __init__(self):
        self.clob = None
        self.key = os.environ.get("MINTER_PRIVATE_KEY")
        self.addr = os.environ.get("MINTER_ADDRESS")
        if MODE == "place":
            raise SystemExit(
                "PLACE DISABLED: CLOB V2 collateral migration is corrected, but "
                "authenticated fill/order reconciliation and strategy edge are unproven"
            )
            if os.environ.get("DEPLOY_REGION") != "eu-west-1":       # M1
                raise SystemExit("M1 geo interlock: place mode only on eu-west-1")
            if not self.key or not self.addr:
                raise SystemExit("MINTER_PRIVATE_KEY/MINTER_ADDRESS missing")
            from eth_account import Account
            if Account.from_key(self.key).address.lower() != self.addr.lower():
                raise SystemExit("MINTER_PRIVATE_KEY does not own MINTER_ADDRESS")
            from live.executor import Clob
            self.clob = Clob(key=self.key, funder=None, sig=0)
            try:
                self.clob.cancel_all_verified()                      # M2 startup sweep
                log.info("startup cancel_all: book swept clean")
            except Exception as e:
                raise SystemExit(f"startup cancel_all not proven: {e}") from e
            from live import chain
            ap = chain.call(chain.CTF, chain.encode_call(
                "isApprovedForAll(address,address)", [self.addr, chain.CTF_EXCHANGE]))
            allowance = int(chain.call(chain.PUSD, chain.encode_call(
                "allowance(address,address)", [self.addr, chain.CTF])), 16) / 1e6
            balance = chain.erc20_balance(chain.PUSD, self.addr)
            if int(ap, 16) != 1:                                     # M7
                raise SystemExit("M7: run live/minter_approve first (exchange approval)")
            if allowance < 8 * MINT_USD:
                raise SystemExit(f"M7: pUSD allowance ${allowance:.0f} < 8x MINT_USD - "
                                 "run live/minter_approve (it grants a large allowance)")
            if balance < 4 * MINT_USD:
                raise SystemExit(f"M7: pUSD balance ${balance:.2f} < 4x MINT_USD")
        self.db = sqlite3.connect(DB)
        self.db.execute("""CREATE TABLE IF NOT EXISTS mint_windows(
            ts REAL, asset TEXT, slug TEXT, minted REAL, sold_up REAL, sold_dn REAL,
            sold_usd REAL, merged REAL, mode TEXT, note TEXT)""")
        self.db.commit()
        self.state: dict[str, dict] = {}
        self.books = BestAskCache()
        self.tok_asset: dict[str, tuple] = {}
        self.resub = asyncio.Event()
        self.day_minted = 0.0
        self.day_key = time.strftime("%Y-%m-%d", time.gmtime())
        self.infra_merge_fails = 0            # M6: infra only, not balance reverts
        self.pos: dict[str, dict] = {}
        self.pos_fresh = 0.0
        self.tasks: set = set()               # hard refs: tasks must never be GC'd
        self.opening: set = set()             # assets with an open_window in flight
        self.feed_counts = collections.Counter()
        self.quote_counts = collections.Counter()
        self.last_feed_log = 0.0
        self.market_feed_at = 0.0
        self.feed_health = FeedHealth()

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
                  "asks": {True: None, False: None}, "last_q": {True: 0.0, False: 0.0},
                  "quote_lock": asyncio.Lock(), "pair_placed_at": 0.0}
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
                    self.books.drop(t)
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
            await self.cancel_pair(st)                     # M5: verified batch cancel
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

    # ---- quoting: paired batches, no old/new cross-pair transition --------
    async def quoter_task(self):
        while True:
            await asyncio.sleep(REQUOTE_S)
            now = time.time()
            for st in list(self.state.values()):
                if (st.get("closing") or st["minted"] <= 0
                        or now < st["base"] + 3 or now > st["close"] - 25):
                    continue
                bu, bd = self.books.get(st["up"]), self.books.get(st["dn"])
                if (not bu or not bd or bu.price is None or bd.price is None
                        or now - self.market_feed_at > FRESH_S):        # M8
                    if any(st["asks"].values()):
                        await self.cancel_pair(st)
                        self.quote_counts["stale_pause"] += 1
                        log.warning("quote paused %s: market feed stale", st["asset"])
                    continue
                prices = guarded_pair_prices(
                    bu.price, bd.price, spread=SPREAD, sum_floor=SUM_FLOOR,
                )
                if prices is None:
                    if any(st["asks"].values()):
                        await self.cancel_pair(st)
                    self.quote_counts["invalid_book_pause"] += 1
                    continue
                px_u, px_d = prices
                plan = plan_pair_quotes(
                    minted=st["minted"], sold_up=st["sold"][True],
                    sold_down=st["sold"][False], price_up=px_u, price_down=px_d,
                    sum_floor=SUM_FLOOR, clip_shares=MIN_SHARES,
                )
                await self.requote_pair(st, plan)

    async def _cancel_pair_unlocked(self, st):
        had_pair = any(st["asks"].values())
        if had_pair:
            rest_ms = max(0, round(1000 * (time.time() - st["pair_placed_at"])))
            self.quote_counts["rest_count"] += 1
            self.quote_counts["rest_ms"] += rest_ms
            self.quote_counts["rest_under15"] += int(rest_ms < 15_000)
        order_ids = [ask[1] for ask in st["asks"].values() if ask and ask[1] != "shadow"]
        if MODE == "place" and self.clob and order_ids:
            await asyncio.to_thread(self.clob.cancel_many_verified, order_ids)
        st["asks"] = {True: None, False: None}
        self.quote_counts["pair_cancel"] += int(had_pair)

    async def cancel_pair(self, st):
        async with st["quote_lock"]:
            await self._cancel_pair_unlocked(st)

    async def requote_pair(self, st, plan: tuple[Quote, ...]):
        async with st["quote_lock"]:
            current = st["asks"]
            if not plan:
                if any(current.values()):
                    await self._cancel_pair_unlocked(st)
                    self.quote_counts["imbalance_pause"] += 1
                    log.warning("quote paused %s: asymmetric or insufficient inventory",
                                st["asset"])
                return
            if all(current[q.side_up] for q in plan):
                old = (current[True][0], current[False][0])
                target = (plan[0].price, plan[1].price)
                if not should_reprice(old, target, time.time() - st["pair_placed_at"]):
                    return
            await self._cancel_pair_unlocked(st)            # old pair is proven gone first
            if st.get("closing"):
                return                                     # close raced the API await
            if MODE != "place" or not self.clob:
                for quote in plan:
                    st["asks"][quote.side_up] = (quote.price, "shadow")
                st["pair_placed_at"] = time.time()
                self.quote_counts["pair_post"] += 1
                log.info("[shadow] ASK PAIR %s %.0f sh @ %.2f/%.2f", st["asset"],
                         plan[0].size, plan[0].price, plan[1].price)
                return
            orders = [
                (st["up"] if quote.side_up else st["dn"], "sell", quote.price, quote.size)
                for quote in plan
            ]
            try:
                order_ids = await asyncio.to_thread(self.clob.place_many, orders, True)
            except Exception as exc:
                # A timeout or partial batch response is unknown money state. A
                # verified wallet sweep is the only safe continuation.
                await asyncio.to_thread(self.clob.cancel_all_verified)
                raise RuntimeError(f"pair placement failed closed: {exc}") from exc
            for quote, order_id in zip(plan, order_ids):
                st["asks"][quote.side_up] = (quote.price, order_id)
            st["pair_placed_at"] = time.time()
            self.quote_counts["pair_post"] += 1

    # ---- feed -------------------------------------------------------------
    async def ws_task(self):
        import websockets
        retry_delay = 0.1
        while True:
            self.resub.clear()
            toks = list(self.tok_asset)
            if not toks:
                await self.resub.wait()
                continue
            connected_at = None
            try:
                self.books.clear()
                async with websockets.connect(MKT_WS, ping_interval=None,
                                              open_timeout=12,
                                              close_timeout=0.1,
                                              max_queue=64) as ws:
                    connected_at = time.monotonic()
                    await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                    last_ping = time.monotonic()
                    log.info("ws subscribed %d tokens (mode=%s mint=$%.0f spread=%.2f)",
                             len(toks), MODE, MINT_USD, SPREAD)
                    while not self.resub.is_set():
                        elapsed = time.monotonic() - last_ping
                        if elapsed >= 10:
                            await ws.send("PING")
                            last_ping = time.monotonic()
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=min(0.5, max(0.1, 10 - elapsed)),
                            )
                        except asyncio.TimeoutError:
                            continue                       # re-check resub fast
                        if raw == "PONG":
                            self.market_feed_at = time.time()
                            continue
                        items = json.loads(raw)
                        received_at = time.time()
                        self.market_feed_at = received_at
                        for it in items if isinstance(items, list) else [items]:
                            if isinstance(it, dict):
                                self.feed_health.observe(it, received_at)
                                self.feed_counts[str(it.get("event_type", "?"))] += 1
                                self.books.apply(it, received_at)
                        if time.monotonic() - self.last_feed_log >= 60:
                            rest_count = self.quote_counts["rest_count"]
                            rest_s = (self.quote_counts["rest_ms"] / rest_count / 1000
                                      if rest_count else 0)
                            under_pct = (100 * self.quote_counts["rest_under15"] / rest_count
                                         if rest_count else 0)
                            quote_events = {
                                key: value for key, value in self.quote_counts.items()
                                if not key.startswith("rest_")
                            }
                            log.info("feed events=%s lag=%s quotes=%s residence=%.1fs "
                                     "under15=%.0f%%", dict(self.feed_counts),
                                     self.feed_health.snapshot(), quote_events, rest_s,
                                     under_pct)
                            self.last_feed_log = time.monotonic()
            except SystemExit:
                raise
            except Exception as e:
                self.feed_health.reconnect()
                if connected_at is not None and time.monotonic() - connected_at >= 5:
                    retry_delay = 0.1
                wait = retry_delay
                retry_delay = min(2.0, retry_delay * 2)
                log.warning("ws reconnect in %.1fs: %s: %s", wait, type(e).__name__, e)
                await asyncio.sleep(wait)

    async def cancel_everything(self):
        if not (MODE == "place" and self.clob):
            return
        try:
            await asyncio.to_thread(self.clob.cancel_all_verified)
            for st in self.state.values():
                st["asks"] = {True: None, False: None}
        except Exception as e:
            log.error("exit cancel_all not proven: %s", e)
            raise
        log.info("exit: all asks cancelled (minted sets merge/redeem on their own)")

    async def main(self):
        log.info("experimental mintbot starting: mode=%s mint=$%.0f/window day-cap=$%.0f "
                 "spread=%.2f sum-floor=%.3f", MODE, MINT_USD, MINT_DAY_CAP,
                 SPREAD, SUM_FLOOR)
        if MODE == "shadow":
            log.warning("shadow validates feeds/quote decisions only; it does not simulate fills or PnL")
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
    except KeyboardInterrupt:
        log.warning("stopped by user")
