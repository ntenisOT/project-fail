#!/usr/bin/env python3
"""
Backtester for Polymarket 5-minute crypto up/down markets, on real historical
trades from the local `copypoly` ClickHouse (read-only) bridged to Polymarket's
Gamma API for window->token mapping and official resolution.

Pipeline:
  1. Enumerate 5-min windows for the chosen assets/date range; via Gamma get the
     Up token id + official winner (cached to disk so reruns are instant).
  2. From ClickHouse trade_history, reconstruct each window's Up-price path and
     extract features (open price, price at T seconds-to-close, hi/lo, #trades).
  3. Sweep entry rules (follow/fade favorite, probability band, momentum/spike,
     close-late) and report win-rate vs price-paid and EV per contract AFTER a
     configurable slippage + fee.

Read-only. No trading, no wallet. Fills are modeled at traded prices (the tape),
which is slightly optimistic; --slippage adds a conservative cushion.

Usage:
  python backtest.py --days 3 --assets btc,eth,sol,xrp
  python backtest.py --days 0.5 --assets btc --slippage 0.01
"""
import argparse, json, os, statistics, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import clickhouse_connect

ASSET_PREFIX = {"btc": "btc-updown-5m", "eth": "eth-updown-5m",
                "sol": "sol-updown-5m", "xrp": "xrp-updown-5m"}
GAMMA = "https://gamma-api.polymarket.com"


# ----------------------------- Gamma bridge -----------------------------------
def gamma_window(prefix, start):
    """Return dict for one window or None. (up_token, winner) via Gamma."""
    slug = f"{prefix}-{start}"
    url = f"{GAMMA}/events?slug={slug}"
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                ev = json.load(r)
            if not ev:
                return None
            m = (ev[0].get("markets") or [{}])[0]
            ids = m.get("clobTokenIds")
            if isinstance(ids, str):
                ids = json.loads(ids)
            if not ids or len(ids) < 2:
                return None
            op = m.get("outcomePrices")
            if isinstance(op, str):
                op = json.loads(op)
            if not m.get("closed") or not op:
                return None  # unresolved -> skip
            return {"slug": slug, "start": start, "up_token": ids[0],
                    "winner_up": 1 if float(op[0]) > 0.5 else 0}
        except Exception:
            time.sleep(0.4)
    return None


def load_cache(path):
    d = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                    d[o["slug"]] = o
                except Exception:
                    pass
    return d


def enumerate_windows(assets, days, cache_path, workers):
    now = int(time.time())
    base = now - (now % 300)
    # windows that ended at least 10 min ago, back `days` days
    latest = base - 600
    earliest = base - int(days * 86400)
    starts = list(range(earliest, latest + 1, 300))
    cache = load_cache(cache_path)
    todo = [(a, s) for a in assets for s in starts
            if f"{ASSET_PREFIX[a]}-{s}" not in cache]
    print(f"windows in range: {len(starts)*len(assets)} | cached: "
          f"{len(starts)*len(assets)-len(todo)} | fetching: {len(todo)}")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    got = 0
    with open(cache_path, "a", encoding="utf-8") as cf:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(gamma_window, ASSET_PREFIX[a], s): (a, s) for a, s in todo}
            for fut in as_completed(futs):
                a, s = futs[fut]
                res = fut.result()
                if res:
                    res["asset"] = a
                    cf.write(json.dumps(res) + "\n")
                    cache[res["slug"]] = res
                    got += 1
                if (got % 200) == 0 and got:
                    print(f"  fetched {got}...")
    # assemble per-asset resolved windows within range
    out = {a: [] for a in assets}
    for a in assets:
        for s in starts:
            o = cache.get(f"{ASSET_PREFIX[a]}-{s}")
            if o and "up_token" in o:
                out[a].append(o)
    for a in assets:
        print(f"  {a}: {len(out[a])} resolved windows")
    return out


# ------------------------- ClickHouse feature query ---------------------------
FEATURE_SQL = """
WITH win AS (SELECT token, start_ts FROM values('token String, start_ts UInt32', {values}))
SELECT token,
  intDiv(count(),2)                    AS ntr,
  argMin(price, ts)                    AS open_px,
  argMaxIf(price, ts, sc>=90)          AS px90,  countIf(sc>=90) AS nc90,
  argMaxIf(price, ts, sc>=60)          AS px60,  countIf(sc>=60) AS nc60,
  argMaxIf(price, ts, sc>=30)          AS px30,  countIf(sc>=30) AS nc30,
  argMaxIf(price, ts, sc>=20)          AS px20,  countIf(sc>=20) AS nc20,
  argMaxIf(price, ts, sc>=10)          AS px10,  countIf(sc>=10) AS nc10,
  argMaxIf(price, ts, sc>=0)           AS pxlast,
  maxIf(price, sc>=20)                 AS hi20,
  minIf(price, sc>=20)                 AS lo20
FROM (
  SELECT tr.token AS token, tr.ts AS ts, tr.price AS price,
         (w.start_ts + 300) - tr.ts AS sc
  FROM (
    SELECT multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
           toUInt32(block_timestamp) AS ts,
           toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
           toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS toks,
           if(toks>0, usdc/toks, 0) AS price
    FROM trade_history
    WHERE block_timestamp >= toDateTime({t0}) AND block_timestamp < toDateTime({t1})
      AND (maker_asset_id IN (SELECT token FROM win) OR taker_asset_id IN (SELECT token FROM win))
  ) tr
  INNER JOIN win w ON tr.token = w.token
)
WHERE sc BETWEEN 0 AND 300
GROUP BY token
"""


def fetch_features(client, windows):
    """windows: list of dicts with up_token,start. Returns {token: feature dict}."""
    if not windows:
        return {}
    values = ",".join(f"('{w['up_token']}', {w['start']})" for w in windows)
    t0 = min(w["start"] for w in windows) - 120
    t1 = max(w["start"] for w in windows) + 420
    sql = FEATURE_SQL.format(values=values, t0=t0, t1=t1)
    cols = ["token", "ntr", "open_px", "px90", "nc90", "px60", "nc60", "px30",
            "nc30", "px20", "nc20", "px10", "nc10", "pxlast", "hi20", "lo20"]
    res = client.query(sql).result_rows
    feats = {}
    for row in res:
        d = dict(zip(cols, row))
        feats[d["token"]] = d
    return feats


# ------------------------------ Strategy sweep --------------------------------
def pnl(side, p, winner_up, slip, fee):
    if side == "up":
        cost = min(p + slip, 0.99)
        return winner_up - cost - fee, cost
    cost = min((1 - p) + slip, 0.99)
    return (1 - winner_up) - cost - fee, cost


def agg(entries):
    """entries: list of (pnl, cost, won_bool). Return summary dict."""
    n = len(entries)
    if n == 0:
        return None
    pnls = [e[0] for e in entries]
    costs = [e[1] for e in entries]
    wins = sum(1 for e in entries if e[2])
    ev = statistics.mean(pnls)
    se = (statistics.pstdev(pnls) / (n ** 0.5)) if n > 1 else float("nan")
    return {"n": n, "win_rate": wins / n, "avg_cost": statistics.mean(costs),
            "ev": ev, "se": se, "t": (ev / se) if se and se == se and se > 0 else 0.0,
            "roi": ev / statistics.mean(costs) if statistics.mean(costs) > 0 else 0.0}


def px_at(f, T):
    return {90: ("px90", "nc90"), 60: ("px60", "nc60"), 30: ("px30", "nc30"),
            20: ("px20", "nc20"), 10: ("px10", "nc10")}[T]


def run_sweep(rows, slip, fee):
    """rows: list of (feature dict, winner_up)."""
    results = []
    T_LIST = [90, 60, 30, 20, 10]

    def valid(f, T):
        pk, nk = px_at(f, T)
        return f["ntr"] > 0 and f[nk] > 0 and 0 < f[pk] < 1

    # A) follow favorite / B) fade favorite at T
    for T in T_LIST:
        pk = px_at({}, T)[0]
        for name, fade in [("follow_fav", False), ("fade_fav", True)]:
            e = []
            for f, w in rows:
                if not valid(f, T):
                    continue
                p = f[pk]
                up_fav = p >= 0.5
                side = ("up" if up_fav else "down")
                if fade:
                    side = ("down" if up_fav else "up")
                pl, cost = pnl(side, p, w, slip, fee)
                won = (side == "up" and w == 1) or (side == "down" and w == 0)
                e.append((pl, cost, won))
            s = agg(e)
            if s:
                results.append({"rule": f"{name}@{T}s", **s})

    # C) probability band -> buy Up when p in band
    for T in [60, 30, 20]:
        pk = px_at({}, T)[0]
        for lo, hi in [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9),
                       (0.9, 0.97), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5)]:
            e = []
            for f, w in rows:
                if not valid(f, T):
                    continue
                p = f[pk]
                if lo <= p < hi:
                    pl, cost = pnl("up", p, w, slip, fee)
                    e.append((pl, cost, w == 1))
            s = agg(e)
            if s and s["n"] >= 20:
                results.append({"rule": f"buyUp@{T}s p[{lo:.2f},{hi:.2f})", **s})

    # D) momentum / spike-follow: bet direction of move from open by >= delta at T
    for T in [60, 30, 20]:
        pk = px_at({}, T)[0]
        for delta in [0.05, 0.10, 0.15, 0.20]:
            e = []
            for f, w in rows:
                if not valid(f, T) or not (0 < f["open_px"] < 1):
                    continue
                p, o = f[pk], f["open_px"]
                if p - o >= delta:
                    side = "up"
                elif o - p >= delta:
                    side = "down"
                else:
                    continue
                pl, cost = pnl(side, p, w, slip, fee)
                won = (side == "up" and w == 1) or (side == "down" and w == 0)
                e.append((pl, cost, won))
            s = agg(e)
            if s and s["n"] >= 20:
                results.append({"rule": f"momentum@{T}s d>={delta:.2f}", **s})

    # E) close-late: market near 50/50 at T, bet recent momentum direction
    for T in [30, 20, 10]:
        pk = px_at({}, T)[0]
        e = []
        for f, w in rows:
            if not valid(f, T) or not (0 < f["open_px"] < 1):
                continue
            p = f[pk]
            if 0.45 <= p <= 0.55:
                side = "up" if p >= f["open_px"] else "down"
                pl, cost = pnl(side, p, w, slip, fee)
                won = (side == "up" and w == 1) or (side == "down" and w == 0)
                e.append((pl, cost, won))
        s = agg(e)
        if s:
            results.append({"rule": f"close_late@{T}s (|p-0.5|<=.05)", **s})

    return results


def describe(rows):
    """Distribution of how 'decided' markets are at 30s to close."""
    close = decided = valid = 0
    base = []
    for f, w in rows:
        base.append(w)
        if f["ntr"] > 0 and f["nc30"] > 0 and 0 < f["px30"] < 1:
            valid += 1
            p = f["px30"]
            if 0.4 <= p <= 0.6:
                close += 1
            if p >= 0.8 or p <= 0.2:
                decided += 1
    n = max(valid, 1)
    return {"windows": len(rows), "valid_at_30s": valid,
            "pct_close_at_30s": 100 * close / n, "pct_decided_at_30s": 100 * decided / n,
            "base_rate_up": 100 * (statistics.mean(base) if base else 0)}


# ---------------------------------- main --------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=3.0)
    ap.add_argument("--assets", default="btc,eth,sol,xrp")
    ap.add_argument("--slippage", type=float, default=0.01)
    ap.add_argument("--fee", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--cache", default="backtest_cache/windows.jsonl")
    ap.add_argument("--out", default="backtest_cache/results.json")
    args = ap.parse_args()
    assets = [a.strip() for a in args.assets.split(",") if a.strip() in ASSET_PREFIX]

    t_start = time.time()
    print(f"=== Backtest: assets={assets} days={args.days} "
          f"slippage={args.slippage} fee={args.fee} ===")
    wins = enumerate_windows(assets, args.days, args.cache, args.workers)

    client = clickhouse_connect.get_client(host="localhost", port=8123,
        username="copypoly", password="copypoly", database="copypoly")

    rows = []
    for a in assets:
        wl = wins[a]
        # chunk to keep query size reasonable
        for i in range(0, len(wl), 400):
            chunk = wl[i:i + 400]
            feats = fetch_features(client, chunk)
            for w in chunk:
                f = feats.get(w["up_token"])
                if f:
                    rows.append((f, w["winner_up"]))
    print(f"windows with trade data: {len(rows)}")

    desc = describe(rows)
    print("\n--- Market shape ---")
    print(f"  base rate Up wins: {desc['base_rate_up']:.1f}%")
    print(f"  windows still 'close' (0.40-0.60) at 30s-to-close: {desc['pct_close_at_30s']:.1f}%")
    print(f"  windows already 'decided' (<=0.20 or >=0.80) at 30s: {desc['pct_decided_at_30s']:.1f}%")

    results = run_sweep(rows, args.slippage, args.fee)
    results.sort(key=lambda r: r["ev"], reverse=True)

    def fmt(r):
        return (f"  {r['rule']:<34} n={r['n']:>5}  win={r['win_rate']*100:>5.1f}%  "
                f"avg_cost={r['avg_cost']:.3f}  EV={r['ev']*100:+6.2f}c  "
                f"t={r['t']:+5.1f}  ROI={r['roi']*100:+6.1f}%")

    print("\n--- TOP rules by EV/contract (after slippage+fee) ---")
    for r in results[:12]:
        print(fmt(r))
    print("\n--- Your 'last 20-30s when close' idea ---")
    for r in results:
        if r["rule"].startswith("close_late"):
            print(fmt(r))
    print("\n--- WORST rules (sanity / fade signals) ---")
    for r in results[-5:]:
        print(fmt(r))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"desc": desc, "params": vars(args), "results": results}, f, indent=2)
    print(f"\nEV = expected profit per $1 contract, in cents. |t|>2 ~ statistically real.")
    print(f"Saved full results to {args.out}. Elapsed {time.time()-t_start:.0f}s.")


if __name__ == "__main__":
    main()
