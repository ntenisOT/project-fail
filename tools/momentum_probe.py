#!/usr/bin/env python3
"""Short-horizon momentum in 5m crypto windows, and whether it survives costs.

Why this exists: every OUTCOME-prediction test came back null. The Polymarket
price is calibrated at T+30/60/120/180 (z within +-0.5 over ~1,955 windows) and
Binance does not lead the settlement TWAP. But the top-margin wallet
(0xce50c96b) does not predict outcomes - its terminal direction is a 53.8% coin
flip. It round-trips inside the window, buying near 0.477 and selling near
0.580, capturing ~5.9c/share on 137k shares. That is PATH prediction over
seconds, which none of the earlier tests measured.

Part 1 measures return autocorrelation on 10s trade-VWAP buckets.
Measured 2026-08-26, 600 BTC windows:

    lookback 10s -> horizon 10s : corr +0.2312  z +28.69
    lookback 10s -> horizon 20s : corr +0.1330  z +16.23
    lookback 30s -> horizon 30s : corr +0.0717  z  +8.25
    quintiles: past 30s -0.1968 -> next -0.0109 ; +0.2025 -> next +0.0211

Every combination is positive momentum. That alone is suspicious, because a
calibrated market is a martingale and a martingale has zero return
autocorrelation. Trade-VWAP momentum can be manufactured by order splitting,
so part 2 is the honest test: enter by LIFTING THE ASK, exit by HITTING THE
BID, and pay the crypto taker fee 0.07*p*(1-p) on both legs.

    threshold 0.02 : net -0.0026/share  (fees eat it)
    threshold 0.05 : net +0.0043/share  (marginal)
    threshold 0.10 : net +0.0196/share over 1,030 trades  (survives)

So the edge is real but small and rare: a >=10c move in 10s fires about 1.7
times per window. The winner does ~232 fills/window, so this single signal does
not reproduce him - he must trade more thresholds, better execution, or
additional signals. Maker execution on either leg would roughly triple the net,
since the round-trip fee is 3.5c against a 4.3c gross.

Usage: python tools/momentum_probe.py [--windows 600] [--asset btc]
"""
from __future__ import annotations

import argparse
import collections
import json
import math

import clickhouse_connect
from clickhouse_connect.driver.external import ExternalData

CACHE = "backtest_cache/set_forensics_windows.jsonl"


def taker_fee(price: float) -> float:
    return 0.07 * price * (1 - price)


def load(asset: str, windows: int, bucket: int):
    rows = [json.loads(line) for line in open(CACHE, encoding="utf-8")]
    rows = sorted((r for r in rows if r["asset"] == asset),
                  key=lambda r: r["start"])[-windows:]
    recs = ["\t".join((r["up_token"], str(r["start"]), "1")) for r in rows]
    external = ExternalData(
        data=("\n".join(recs) + "\n").encode(), file_name="wt", fmt="TabSeparated",
        structure="token String, start_ts UInt32, side UInt8")
    t0, t1 = rows[0]["start"] - 600, rows[-1]["start"] + 900
    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
        settings={"max_execution_time": 900, "max_memory_usage": 8_000_000_000,
                  "max_query_size": 300_000_000})
    query = f"""
    WITH legs AS (
      SELECT multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
             toUnixTimestamp(block_timestamp) AS ts, maker_asset_id='0' AS maker_bought,
             toFloat64(if(maker_asset_id='0', taker_amount_filled,
                          maker_amount_filled))/1e6 AS sh,
             toFloat64(if(maker_asset_id='0', maker_amount_filled,
                          taker_amount_filled))/1e6 AS usd
      FROM trade_history
      WHERE block_timestamp >= toDateTime({t0}) AND block_timestamp < toDateTime({t1}))
    SELECT w.start_ts, intDiv(l.ts-w.start_ts,{bucket})*{bucket} AS b,
      sumIf(l.usd, l.maker_bought)/nullIf(sumIf(l.sh, l.maker_bought),0) AS bid_px,
      sumIf(l.usd, NOT l.maker_bought)/nullIf(sumIf(l.sh, NOT l.maker_bought),0) AS ask_px,
      sum(l.usd)/sum(l.sh) AS mid
    FROM legs l INNER JOIN wt w ON l.token = w.token
    WHERE l.ts-w.start_ts BETWEEN 0 AND 299 AND l.sh > 0
    GROUP BY w.start_ts, b ORDER BY w.start_ts, b"""
    series: dict = collections.defaultdict(dict)
    for start, b, bid, ask, mid in client.query(
            query, external_data=external).result_rows:
        series[start][b] = (bid, ask, mid)
    return series


def autocorr(series, lookback: int, horizon: int, bucket: int):
    xs, ys = [], []
    for _start, d in series.items():
        for b in range(lookback, 300 - horizon, bucket):
            p0, p1, p2 = d.get(b - lookback), d.get(b), d.get(b + horizon)
            if not p0 or not p1 or not p2:
                continue
            if None in (p0[2], p1[2], p2[2]):
                continue
            xs.append(p1[2] - p0[2])
            ys.append(p2[2] - p1[2])
    n = len(xs)
    if n < 200:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    r = sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0
    return n, r, r * math.sqrt(n - 1)


def tradeable(series, threshold: float, horizon: int, bucket: int):
    trades = 0
    gross = net = 0.0
    for _start, d in series.items():
        for b in range(bucket, 300 - horizon, bucket):
            p0, p1, p2 = d.get(b - bucket), d.get(b), d.get(b + horizon)
            if not p0 or not p1 or not p2:
                continue
            if None in (p0[2], p1[2]) or p2[0] is None or p1[1] is None:
                continue
            if p1[2] - p0[2] < threshold:
                continue
            entry, exit_ = p1[1], p2[0]
            if not (0 < entry < 1 and 0 < exit_ < 1):
                continue
            trades += 1
            gross += exit_ - entry
            net += exit_ - entry - taker_fee(entry) - taker_fee(exit_)
    return trades, gross, net


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--windows", type=int, default=600)
    ap.add_argument("--bucket", type=int, default=10)
    ap.add_argument("--clip", type=float, default=6.0)
    args = ap.parse_args()
    series = load(args.asset, args.windows, args.bucket)
    print(f"windows={len(series)} bucket={args.bucket}s")

    print(f"\n{'lookback':>9} {'horizon':>8} {'n':>7} {'corr':>9} {'z':>8}")
    for lookback in (10, 20, 30, 60):
        for horizon in (10, 20, 30, 60):
            res = autocorr(series, lookback, horizon, args.bucket)
            if res:
                n, r, z = res
                print(f"{lookback:>9} {horizon:>8} {n:>7,} {r:>+9.4f} {z:>+8.2f}")

    print(f"\n{'thresh':>7} {'H':>4} {'trades':>7} {'gross/sh':>9} {'net/sh':>9} {'total$':>9}")
    for threshold in (0.02, 0.05, 0.10):
        for horizon in (10, 20, 30):
            trades, gross, net = tradeable(series, threshold, horizon, args.bucket)
            if trades > 50:
                print(f"{threshold:>7.2f} {horizon:>4} {trades:>7,} "
                      f"{gross/trades:>+9.4f} {net/trades:>+9.4f} "
                      f"{net*args.clip:>+9,.0f}")
    print(f"\nnet is after LIFTING THE ASK in and HITTING THE BID out, plus the")
    print(f"crypto taker fee on both legs ({2*taker_fee(0.5):.4f} round trip at p=0.50).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
