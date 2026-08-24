"""Paper trader runner — 11 strategies in parallel on one live feed. PAPER ONLY.

Feeds (signal only; no capital anywhere but Polymarket):
  - ws-live-data: crypto_prices_chainlink (60s TWAP) + crypto_prices (Binance spot)
  - Deribit WS: deribit_price_index.<asset>_usd
  - CLOB market WS: last_trade_price (fills) + book (lock_arb best asks)
Fair value per strategy is computed here and passed into the engine.
Run: python -m paper.run
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
import urllib.request

import websockets

from paper import envload
envload.load()

from paper import report
from paper.engine import PaperWindow, TWAP, fair_up
from paper.ledger import Ledger
from paper.live_gate import LiveGate
from paper.notify import notifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("paper.run")

ASSETS = {"btc": "btc-updown-5m", "eth": "eth-updown-5m", "sol": "sol-updown-5m", "xrp": "xrp-updown-5m"}
_want = os.environ.get("PAPER_ASSETS")
if _want:
    ASSETS = {a: p for a, p in ASSETS.items() if a in _want.split(",")}
BINANCE_SYM = {"btcusdt": "btc", "ethusdt": "eth", "solusdt": "sol", "xrpusdt": "xrp"}
DERIBIT_IDX = {"btc": "btc_usd", "eth": "eth_usd", "sol": "sol_usd", "xrp": "xrp_usd"}

# 10 signal strategies (lock_arb handled separately)
STRATEGIES = [
    {"name": "hold",        "mode": "hold",      "signal": "twap",    "confirm": [],                    "spread": 0.03, "size_mode": "fixed"},
    {"name": "roundtrip",   "mode": "roundtrip", "signal": "twap",    "confirm": [],                    "spread": 0.03, "size_mode": "fixed"},
    {"name": "rt_wide",     "mode": "roundtrip", "signal": "twap",    "confirm": [],                    "spread": 0.05, "size_mode": "fixed"},
    {"name": "opp_size",    "mode": "roundtrip", "signal": "twap",    "confirm": [],                    "spread": 0.03, "size_mode": "opp"},
    {"name": "neutral",     "mode": "roundtrip", "signal": "mid",     "confirm": [],                    "spread": 0.03, "size_mode": "fixed"},
    {"name": "twap_confirm","mode": "roundtrip", "signal": "twap",    "confirm": ["binance", "deribit"],"spread": 0.03, "size_mode": "fixed"},
    {"name": "twap_binance","mode": "roundtrip", "signal": "twap",    "confirm": ["binance"],           "spread": 0.03, "size_mode": "fixed"},
    {"name": "twap_deribit","mode": "roundtrip", "signal": "twap",    "confirm": ["deribit"],           "spread": 0.03, "size_mode": "fixed"},
    {"name": "binance_only","mode": "roundtrip", "signal": "binance", "confirm": [],                    "spread": 0.03, "size_mode": "fixed"},
    {"name": "deribit_only","mode": "roundtrip", "signal": "deribit", "confirm": [],                    "spread": 0.03, "size_mode": "fixed"},
    # order-size experiment: clones of the leading arm with bigger participation
    {"name": "td_f40",      "mode": "roundtrip", "signal": "twap",    "confirm": ["deribit"],           "spread": 0.03, "size_mode": "fixed", "f": 0.4},
    {"name": "td_inv600",   "mode": "roundtrip", "signal": "twap",    "confirm": ["deribit"],           "spread": 0.03, "size_mode": "fixed", "maxinv": 600},
    # exit-first (xf) twins: SAME signals, winner-style inventory policy --
    # entry-anchored asks + forced near-close taker exit (carry ~0 to settlement)
    {"name": "xf_roundtrip","mode": "roundtrip", "signal": "twap",    "confirm": [],                    "spread": 0.03, "size_mode": "fixed", "xf": True},
    {"name": "xf_opp",      "mode": "roundtrip", "signal": "twap",    "confirm": [],                    "spread": 0.03, "size_mode": "opp",   "xf": True},
    {"name": "xf_neutral",  "mode": "roundtrip", "signal": "mid",     "confirm": [],                    "spread": 0.03, "size_mode": "fixed", "xf": True},
    {"name": "xf_twap_con", "mode": "roundtrip", "signal": "twap",    "confirm": ["binance", "deribit"],"spread": 0.03, "size_mode": "fixed", "xf": True},
    {"name": "xf_twap_bin", "mode": "roundtrip", "signal": "twap",    "confirm": ["binance"],           "spread": 0.03, "size_mode": "fixed", "xf": True},
    {"name": "xf_twap_der", "mode": "roundtrip", "signal": "twap",    "confirm": ["deribit"],           "spread": 0.03, "size_mode": "fixed", "xf": True},
    {"name": "xf_binance",  "mode": "roundtrip", "signal": "binance", "confirm": [],                    "spread": 0.03, "size_mode": "fixed", "xf": True},
    {"name": "xf_deribit",  "mode": "roundtrip", "signal": "deribit", "confirm": [],                    "spread": 0.03, "size_mode": "fixed", "xf": True},
    # pair_mm: trades the SUM, not the direction (the measured winner style).
    # fair(token) = 1 - last price of the OTHER side  =>  bid fills only when
    # up+down < 1-spread (buy sets below face, maker), asks when sum > 1+spread.
    # Paired inventory settles at face value in engine.settle() by construction.
    {"name": "pair_mm",     "mode": "roundtrip", "signal": "pair",    "confirm": [],                    "spread": 0.02, "size_mode": "fixed", "pair_balance": True},
]
NAMES = [s["name"] for s in STRATEGIES]
F, MAXINV, MINSIG = 0.2, 200, 0.05
REQUOTE = float(os.environ.get("PAPER_REQUOTE", "1.0"))   # s between quote refreshes (fill model v2)
LOCK_MARGIN, TAKER_RATE = 0.002, 0.07  # capture only sets with NET edge (after taker fees) > margin

CL_WS = "wss://ws-live-data.polymarket.com"
DERIBIT_WS = "wss://www.deribit.com/ws/api/v2"
MKT_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA = "https://gamma-api.polymarket.com"


def gamma_tokens(prefix, base):
    try:
        req = urllib.request.Request(f"{GAMMA}/events?slug={prefix}-{base}", headers={"User-Agent": "Mozilla/5.0"})
        ev = json.load(urllib.request.urlopen(req, timeout=10))
        m = (ev[0].get("markets") or [{}])[0]
        ids = m.get("clobTokenIds")
        ids = json.loads(ids) if isinstance(ids, str) else ids
        return ids if ids and len(ids) >= 2 else None
    except Exception:
        return None


class State:
    def __init__(self):
        self.twap = {a: TWAP() for a in ASSETS}
        self.binance: dict[str, float] = {}
        self.deribit: dict[str, float] = {}
        self.wref = {a: {"twap": None, "binance": None, "deribit": None} for a in ASSETS}
        self.win = {n: {} for n in NAMES}
        self.lock = {a: None for a in ASSETS}          # lock_arb accumulator per asset
        self.split = {a: None for a in ASSETS}         # split_sell accumulator per asset
        self.best_bid: dict[str, float] = {}
        self.tok_map: dict[str, tuple[str, bool]] = {}
        self.best_ask: dict[str, float] = {}
        self.last_price: dict[str, float] = {}
        self.tokens: set[str] = set()
        self.tokens_changed = asyncio.Event()
        self.ledger = Ledger()
        self.gate = LiveGate()
        self.notify = notifier()
        self.counts = collections.Counter()


S = State()


def src_fair(src, a):
    if src == "twap":
        return fair_up(S.twap[a].now(), S.wref[a]["twap"])
    if src == "binance":
        return fair_up(S.binance.get(a), S.wref[a]["binance"])
    if src == "deribit":
        return fair_up(S.deribit.get(a), S.wref[a]["deribit"])
    return None


def strat_fair_up(strat, a):
    prim = src_fair(strat["signal"], a)
    if prim is None:
        return None
    for c in strat["confirm"]:
        cf = src_fair(c, a)
        if cf is None or (cf - 0.5) * (prim - 0.5) <= 0:   # missing or disagrees on direction
            return None
    return prim


async def ws_live_task():
    while True:
        try:
            async with websockets.connect(CL_WS, ping_interval=None, open_timeout=12) as ws:
                for topic in ("crypto_prices_chainlink", "crypto_prices"):
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": topic, "type": "update"}]}))
                lp = 0.0
                while True:
                    if time.time() - lp > 5:
                        await ws.send("PING"); lp = time.time()
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    try:
                        d = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue          # PONG / non-JSON keepalive
                    p = d.get("payload") or {}
                    if d.get("topic") == "crypto_prices_chainlink":
                        a = {v: k for k, v in DERIBIT_IDX.items()}.get(p.get("symbol", "").replace("/", "_"))
                        if a and p.get("value"):
                            S.twap[a].add(time.time(), float(p["value"]))
                    elif d.get("topic") == "crypto_prices":
                        a = BINANCE_SYM.get(p.get("symbol"))
                        if a and p.get("value"):
                            S.binance[a] = float(p["value"])
        except Exception as e:
            log.warning("ws-live reconnect: %s", e.__class__.__name__)
            await asyncio.sleep(2)


async def deribit_task():
    chans = [f"deribit_price_index.{DERIBIT_IDX[a]}" for a in ASSETS]
    inv = {DERIBIT_IDX[a]: a for a in ASSETS}
    while True:
        try:
            async with websockets.connect(DERIBIT_WS, ping_interval=None, open_timeout=12) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "public/subscribe",
                                          "params": {"channels": chans}}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=20)
                    try:
                        d = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if d.get("method") == "subscription":
                        pr = d["params"]; data = pr.get("data") or {}
                        a = inv.get(data.get("index_name"))
                        if a and data.get("price"):
                            S.deribit[a] = float(data["price"])
        except Exception as e:
            log.warning("deribit reconnect: %s", e.__class__.__name__)
            await asyncio.sleep(3)


async def window_task():
    loop = asyncio.get_event_loop()
    while True:
        now = time.time()
        base = int(now - (now % 300))
        for a, prefix in ASSETS.items():
            ref = S.win[NAMES[0]].get(a)
            rolled = ref is not None and now >= ref.end
            if rolled and not ref.settled:
                endtw = S.twap[a].at(ref.end) or S.twap[a].now()
                outcome = 1 if (endtw is not None and S.wref[a]["twap"] and endtw >= S.wref[a]["twap"]) else 0
                for n in NAMES:
                    w = S.win[n].get(a)
                    if w and not w.settled:
                        w.settled = True
                        S.ledger.record_settlement(now, n, a, w.slug, w.settle(outcome))
                lk = S.lock.get(a)
                if lk and lk["n"] > 0:
                    S.ledger.record_settlement(now, "lock_arb", a, lk["slug"],
                        {"cash": lk["profit"], "residual": 0.0, "pnl": lk["profit"], "capital": lk["peak"],
                         "buys": lk["n"], "sells": lk["n"], "resid_shares": 0.0, "n_fills": lk["n"], "outcome_up": outcome})
                sp = S.split.get(a)
                if sp and sp["n"] > 0:
                    S.ledger.record_settlement(now, "split_sell", a, sp["slug"],
                        {"cash": sp["profit"], "residual": 0.0, "pnl": sp["profit"], "capital": sp["peak"],
                         "buys": sp["n"], "sells": sp["n"], "resid_shares": 0.0, "n_fills": sp["n"], "outcome_up": outcome})
                log.info("settled %s %s outcome=%d", a, ref.slug, outcome)
            if ref is None or rolled:
                ids = await loop.run_in_executor(None, gamma_tokens, prefix, base)
                if ids:
                    slug = f"{prefix}-{base}"
                    S.wref[a] = {"twap": S.twap[a].at(base), "binance": S.binance.get(a), "deribit": S.deribit.get(a)}
                    for s in STRATEGIES:
                        w = PaperWindow(a, slug, base, s["spread"], s.get("f", F), s.get("maxinv", MAXINV),
                                        MINSIG if s["signal"] not in ("mid", "pair") else -1.0,
                                        mode=s["mode"], size_mode=s["size_mode"], requote=REQUOTE,
                                        exit_first=s.get("xf", False), pair_balance=s.get("pair_balance", False))
                        w.up_tok, w.down_tok = ids[0], ids[1]
                        S.win[s["name"]][a] = w
                    S.lock[a] = {"slug": slug, "up": ids[0], "dn": ids[1], "cost": 0.0, "profit": 0.0, "n": 0, "peak": 0.0}
                    S.split[a] = {"slug": slug, "up": ids[0], "dn": ids[1], "cost": 0.0, "profit": 0.0, "n": 0, "peak": 0.0}
        tok_map, toks = {}, set()
        for a, w in S.win[NAMES[0]].items():
            if w.up_tok:
                tok_map[w.up_tok] = (a, True); tok_map[w.down_tok] = (a, False)
                toks.add(w.up_tok); toks.add(w.down_tok)
        S.tok_map = tok_map
        if toks != S.tokens:
            S.tokens = toks; S.tokens_changed.set()
        await asyncio.sleep(5)


def check_split(a):
    sp = S.split.get(a)
    if not sp:
        return
    bu, bd = S.best_bid.get(sp["up"]), S.best_bid.get(sp["dn"])
    if bu is None or bd is None:
        return
    s = bu + bd
    # mint $1 -> SELL both sides at the bids = taker on both legs
    fee = TAKER_RATE * (bu * (1 - bu) + bd * (1 - bd))
    net = s - 1.0 - fee
    if net > LOCK_MARGIN:
        size = F * 50
        sp["cost"] += size * 1.0                   # mint cost, recycled on the immediate sells
        sp["profit"] += size * net
        sp["peak"] = max(sp["peak"], size * 1.0)
        sp["n"] += 1


def check_lock(a):
    lk = S.lock.get(a)
    if not lk:
        return
    ua, da = S.best_ask.get(lk["up"]), S.best_ask.get(lk["dn"])
    if ua is None or da is None:
        return
    s = ua + da
    # both legs lift the ask = TAKER: fee = 0.07*p*(1-p) per share on each leg
    fee = TAKER_RATE * (ua * (1 - ua) + da * (1 - da))
    net = 1.0 - s - fee
    if net > LOCK_MARGIN:                      # YES+NO cheap enough to survive fees: buy both, merge to $1
        size = F * 50
        cost = size * (s + fee)
        lk["cost"] += cost
        lk["profit"] += size * net
        lk["peak"] = max(lk["peak"], cost)     # capital recycles via merge -> bankroll = peak single lock
        lk["n"] += 1


def handle_event(it):
    et = it.get("event_type", "?")
    S.counts[et] += 1
    if et == "book":
        tok = it.get("asset_id"); asks = it.get("asks") or []
        bids = it.get("bids") or []
        if asks:
            S.best_ask[tok] = min(float(x["price"]) for x in asks)
        if bids:
            S.best_bid[tok] = max(float(x["price"]) for x in bids)
        info = S.tok_map.get(tok)
        if info and (asks or bids):
            check_lock(info[0])
            check_split(info[0])
        return
    if et != "last_trade_price":
        return
    info = S.tok_map.get(it.get("asset_id"))
    if not info:
        return
    try:
        price = float(it.get("price", 0)); size = float(it.get("size", 0))
    except (TypeError, ValueError):
        return
    if price <= 0 or size <= 0:
        return
    a, is_up = info
    tok = it.get("asset_id")
    now = time.time()
    prev_mid = S.last_price.get(tok)      # mid BEFORE this trade (for neutral MM)
    is_sell = str(it.get("side", "")).upper() == "SELL"
    for s in STRATEGIES:
        w = S.win[s["name"]].get(a)
        if not w or w.settled:
            continue
        if s["signal"] == "mid":
            fair_tok = prev_mid
        elif s["signal"] == "pair":
            other = w.down_tok if is_up else w.up_tok
            po = S.last_price.get(other)
            fair_tok = None if po is None else max(0.02, min(0.98, 1.0 - po))
        else:
            fu = strat_fair_up(s, a)
            fair_tok = None if fu is None else (fu if is_up else 1 - fu)
        pre = None
        if S.gate.enabled(s["name"]):
            qq = w.q[is_up]; pre = (qq["bid"], qq["ask"])
        rec = w.on_trade(now, is_up, price, size, is_sell, fair_tok)
        if rec:
            S.ledger.record_fill(now, s["name"], a, w.slug, rec)
        if pre is not None:
            qq = w.q[is_up]
            if (qq["bid"], qq["ask"]) != pre:
                S.gate.emit_quotes(s["name"], a, w.slug, tok, is_up, qq["bid"], qq["ask"], w.deployed)
    S.last_price[tok] = price


async def market_task():
    while True:
        if not S.tokens:
            await asyncio.sleep(1); continue
        toks = list(S.tokens)
        try:
            async with websockets.connect(MKT_WS, ping_interval=None, open_timeout=12) as ws:
                await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                S.tokens_changed.clear()
                while not S.tokens_changed.is_set():
                    try:
                        d = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                    except (asyncio.TimeoutError, json.JSONDecodeError):
                        continue
                    for it in (d if isinstance(d, list) else [d]):
                        handle_event(it)
        except Exception as e:
            log.warning("market ws reconnect: %s", e.__class__.__name__)
            await asyncio.sleep(2)


async def heartbeat():
    while True:
        await asyncio.sleep(20)
        feeds = {a: (round(S.twap[a].now() or 0, 1), S.binance.get(a), S.deribit.get(a)) for a in ASSETS}
        acts = {n: sum(w.buys + w.sells for w in S.win[n].values()) for n in NAMES}
        locks = sum(lk["n"] for lk in S.lock.values() if lk)
        log.info("hb | feeds(twap,bin,der)=%s | events=%s | acts=%s | lock_caps=%d",
                 feeds, dict(S.counts), acts, locks)


async def summary_task():
    mins = float(os.environ.get("PAPER_SUMMARY_MINS", "15"))
    delay = 120.0                     # first report ~2 min after start, then every interval
    while True:
        await asyncio.sleep(delay)
        delay = mins * 60
        txt = report.text()
        for line in txt.splitlines():
            log.info(line)
        S.notify.send(report.tg_text(), pre=True)


async def live_report_task():
    import sqlite3
    mins = float(os.environ.get("PAPER_TG_MINS", "10"))
    delay = 150.0                     # first report ~2.5 min after start, then every interval
    while True:
        await asyncio.sleep(delay)
        delay = mins * 60
        en = sorted((S.gate._config().get("enabled") or []))
        if not (S.gate.active and en):
            continue
        real = os.path.exists("live/live.db")
        lines = [(f"LIVE · REAL fills · {time.strftime('%H:%M')}" if real else
                  f"LIVE-CANDIDATES · paper sim · {time.strftime('%H:%M')}")]
        try:
            if real:
                ld = sqlite3.connect("live/live.db")
                day0 = time.time() // 86400 * 86400
                n, cash = ld.execute("""SELECT count(*), COALESCE(sum(CASE WHEN side='BUY' THEN -usd ELSE usd END),0)
                                        FROM live_fills WHERE ts>=?""", (day0,)).fetchone()
                no, nc = ld.execute("SELECT sum(action='place'), sum(action='cancel') FROM live_orders WHERE ts>=?", (day0,)).fetchone()
                lines.append(f"today {n}f · net {cash:+.2f}$ · ord {no or 0}/{nc or 0}")
                ld.close()
            for st in en:
                s = report.snapshot_one(S.ledger.db, st)
                nf = s['buys'] + s['sells']
                avg = s['volume'] / nf if nf else 0.0
                lines.append(f"{st[:12]:<12}{s['pnl']:>+7.1f}$ {s['win_rate']*100:>3.0f}% {s['settled']:>3}w b{s['budget']:>4.0f}")
                lines.append(f"  vol {s['volume']:>5.0f}$ avg {avg:.2f}$ s/b {s['sell_buy']:.2f}")
            if not real:
                lines.append("NO real orders - executor OFF.")
                lines.append("(these 2 arms are queued for live)")
        except Exception as e:
            lines.append(f"(report error: {e.__class__.__name__})")
        S.notify.send("\n".join(lines), pre=True)


async def main():
    log.info("paper trader (22 arms + lock_arb + split_sell) starting | fill model v2 (requote=%.1fs, min-post=5sh, taker-only fees->maker 0) | assets=%s (PAPER - no real orders)", REQUOTE, list(ASSETS))
    S.notify.send("paper trader started: 11-strategy A/B, fill model v2 (PAPER - no real orders)")
    await asyncio.gather(ws_live_task(), deribit_task(), window_task(), market_task(), heartbeat(), summary_task(), live_report_task())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("stopped.")
