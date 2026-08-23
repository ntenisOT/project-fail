#!/usr/bin/env python3
"""
mm_sim v2 — replica of the winners' strategy, tested on REAL recorded data.

Strategy (from profiling): passively BID the side the Chainlink 60s TWAP favors,
accumulate as sellers hit us, hold to settlement. Edge exists only if the TWAP
LEADS the market price (so the favoured side is still cheap when we buy it).

Inputs:
  - out/refprices-*.jsonl  -> Chainlink price series -> 60s TWAP fair value
  - recorded window slugs (out/depth-*.jsonl) -> Gamma -> tokens + official winner
  - ClickHouse trade_history -> the actual trades that would fill our resting bids

Fill model: for each aggressive SELL of the favoured token at price p<=(fair-spread),
we buy fill_frac*size at p (fill_frac = queue position = your speed). Hold to $1/$0.
Baseline: same, but "favoured" chosen by market mid instead of TWAP -> isolates the
TWAP's predictive lead. Read-only / paper.
"""
import json, glob, sys, time, urllib.request, statistics, bisect
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import clickhouse_connect

CL_SYMBOL = {"btc": "btc/usd", "eth": "eth/usd", "sol": "sol/usd", "xrp": "xrp/usd"}
PREFIX = {"btc": "btc-updown-5m", "eth": "eth-updown-5m", "sol": "sol-updown-5m", "xrp": "xrp-updown-5m"}
S = {"max_query_size": 300_000_000, "max_ast_elements": 20_000_000, "max_expanded_ast_elements": 20_000_000}
K = 400.0   # TWAP skew sensitivity: 0.1% lead -> ~0.9 fair


def load_jsonl(pat):
    out = []
    for fn in glob.glob(pat):
        with open(fn, encoding="utf-8") as f:
            for line in f:
                try: out.append(json.loads(line))
                except: pass
    return out


def gamma_window(slug):
    try:
        req = urllib.request.Request(f"https://gamma-api.polymarket.com/events?slug={slug}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as r:
            ev = json.load(r)
        if not ev: return None
        m = (ev[0].get("markets") or [{}])[0]
        ids = m.get("clobTokenIds"); ids = json.loads(ids) if isinstance(ids, str) else ids
        op = m.get("outcomePrices"); op = json.loads(op) if isinstance(op, str) else op
        if not ids or not op or not m.get("closed"): return None
        return {"up": ids[0], "down": ids[1], "winner_up": 1 if float(op[0]) > 0.5 else 0}
    except Exception:
        return None


class TWAP:
    """60s rolling TWAP of chainlink price for one asset."""
    def __init__(self, ticks):
        ticks = sorted(ticks, key=lambda z: z[0])
        self.ts = [t for t, _ in ticks]
        self.v = [v for _, v in ticks]

    def at(self, t):  # mean of values in (t-60, t]
        hi = bisect.bisect_right(self.ts, t)
        lo = bisect.bisect_left(self.ts, t - 60, 0, hi)
        if hi <= lo: return None
        return statistics.fmean(self.v[lo:hi])


def fair_up_from_twap(twap_now, start_ref):
    if twap_now is None or not start_ref: return None
    return min(0.98, max(0.02, 0.5 + K * (twap_now - start_ref) / start_ref))


def simulate(trades, winner_up, fair_fn, spread, f):
    """trades: list of (ts, is_up, price, size, aggressor_sell). fair_fn(ts)->fair_up or None."""
    pos_up = pos_dn = cash = 0.0
    peak = 0.0
    for ts, is_up, p, q, sell in trades:
        if not sell:            # we bid; only aggressive sells fill us
            continue
        fu = fair_fn(ts)
        if fu is None: continue
        fair_tok = fu if is_up else (1 - fu)
        if fair_tok > 0.5 and p <= fair_tok - spread:   # favoured side, cheap vs our bid
            fill = f * q
            cash -= fill * p
            if is_up: pos_up += fill
            else: pos_dn += fill
            peak = max(peak, -cash)     # cumulative capital out (buys only here)
    cash += pos_up * winner_up + pos_dn * (1 - winner_up)   # settle
    return cash, max(peak, 1.0)


def main():
    dep = load_jsonl("out/depth-*.jsonl")
    ref = load_jsonl("out/refprices-*.jsonl")
    # windows
    wins = {}
    for r in dep:
        wins.setdefault(r["slug"], r["asset"])
    print(f"recorded windows: {len(wins)}")

    # resolve window meta via Gamma (cache)
    cache = {}
    try:
        for line in open("backtest_cache/windows_v2.jsonl", encoding="utf-8"):
            o = json.loads(line); cache[o["slug"]] = o
    except FileNotFoundError:
        pass
    import os
    os.makedirs("backtest_cache", exist_ok=True)
    meta = {}
    with open("backtest_cache/windows_v2.jsonl", "a", encoding="utf-8") as cf:
        for slug, asset in wins.items():
            if slug in cache:
                g = cache[slug]
            else:
                g = gamma_window(slug)
                if g:
                    g["slug"] = slug; g["asset"] = asset
                    g["start"] = int(slug.rsplit("-", 1)[1])
                    cf.write(json.dumps(g) + "\n")
            if g and "up" in g:
                g.setdefault("start", int(slug.rsplit("-", 1)[1]))
                g.setdefault("asset", asset)
                meta[slug] = g
    print(f"resolved windows: {len(meta)}")

    # TWAP per asset
    cl = {a: [] for a in CL_SYMBOL}
    sym2a = {v: k for k, v in CL_SYMBOL.items()}
    for r in ref:
        if r.get("src") == "chainlink" and r.get("symbol") in sym2a and r.get("value"):
            cl[sym2a[r["symbol"]]].append((r["ts"], float(r["value"])))
    twap = {a: TWAP(t) for a, t in cl.items() if t}

    # trades from ClickHouse for all window tokens
    toks = []
    for g in meta.values():
        toks += [g["up"], g["down"]]
    starts = [g["start"] for g in meta.values()]
    t0, t1 = min(starts) - 120, max(starts) + 400
    toklist = "'" + "','".join(toks) + "'"
    c = clickhouse_connect.get_client(host="localhost", port=8123,
        username="copypoly", password="copypoly", database="copypoly")
    q = f"""
    SELECT multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
           toUInt32(block_timestamp) AS ts,
           toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
           toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS toks,
           taker_asset_id != '0' AS taker_sold
    FROM trade_history
    WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
      AND (maker_asset_id IN ({toklist}) OR taker_asset_id IN ({toklist}))
    """
    rawtrades = c.query(q, settings=S).result_rows
    bytok = {}
    for token, ts, usdc, tks, sold in rawtrades:
        if tks <= 0: continue
        bytok.setdefault(token, []).append((ts, usdc / tks, tks, bool(sold)))
    print(f"trades pulled: {len(rawtrades)}")

    # build per-window trade streams
    windows = []
    for slug, g in meta.items():
        a = g["asset"]; start = g["start"]
        up_tr = bytok.get(g["up"], []); dn_tr = bytok.get(g["down"], [])
        stream = [(ts, True, p, q, s) for ts, p, q, s in up_tr] + \
                 [(ts, False, p, q, s) for ts, p, q, s in dn_tr]
        stream = [t for t in stream if start <= t[0] < start + 300]
        stream.sort()
        if not stream: continue
        tw = twap.get(a)
        start_ref = tw.at(start) if tw else None
        if not start_ref: continue
        windows.append((slug, g, stream, start_ref, tw))
    print(f"windows simulable: {len(windows)}\n")

    def run(mode, spread, f):
        pnls, caps = [], []
        for slug, g, stream, start_ref, tw in windows:
            if mode == "twap":
                fair_fn = lambda ts, tw=tw, sr=start_ref: fair_up_from_twap(tw.at(ts), sr)
            else:  # market-mid baseline: fair_up = last up-trade price
                ups = [(ts, p) for ts, is_up, p, q, s in stream if is_up]
                def fair_fn(ts, ups=ups):
                    i = bisect.bisect_right([u[0] for u in ups], ts) - 1
                    return ups[i][1] if i >= 0 else 0.5
            pnl, cap = simulate(stream, g["winner_up"], fair_fn, spread, f)
            pnls.append(pnl); caps.append(cap)
        tot = sum(pnls); avgcap = statistics.fmean(caps)
        winp = 100 * sum(1 for x in pnls if x > 0) / len(pnls)
        roc = tot / sum(caps) * 100 if sum(caps) else 0
        return tot, winp, avgcap, roc

    print(f"{'model':>8}{'spread':>7}{'fillf':>6}{'tot_pnl$':>10}{'win%':>7}{'avgcap$':>9}{'ROC%':>8}")
    for mode in ["twap", "mid"]:
        for spread in [0.02, 0.05, 0.10]:
            for f in [0.1, 0.3, 1.0]:
                tot, winp, avgcap, roc = run(mode, spread, f)
                print(f"{mode:>8}{spread:>7.2f}{f:>6.1f}{tot:>10.2f}{winp:>6.0f}%{avgcap:>9.2f}{roc:>7.1f}%")
    print("\nROC% = total pnl / total capital across the sampled windows.")
    print("If 'twap' >> 'mid', the Chainlink TWAP leads price and the edge is real.")


if __name__ == "__main__":
    main()
