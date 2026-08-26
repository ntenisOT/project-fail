#!/usr/bin/env python3
"""Traded pair cost as a function of offset from the 5-minute window open.

Public-tape measurement (no wallet filter): for every resolved window, bucket
all fills by seconds relative to the window's start timestamp and report the
volume-weighted Up and Down price plus their sum. A minted complete set costs
$1.00, so the pair sum IS the gross value of selling one set at that moment.

Measured 2026-08-26 over 600 BTC windows / 3.9M trades:
    -180..0s  0.998   |  0..120s ~1.00  |  120..180s 1.02
    180..240s 1.04-1.07 | 240..300s 1.08-1.11

The premium is concentrated in the last two minutes. Selling minted sets
early is close to par; a >=1.00 ask before T+120 sits above the market and
simply does not fill.

Usage: python tools/pair_cost_curve.py [--asset btc] [--windows 600]
"""
from __future__ import annotations

import argparse
import json

import clickhouse_connect
from clickhouse_connect.driver.external import ExternalData

CACHE = "backtest_cache/set_forensics_windows.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--windows", type=int, default=600)
    ap.add_argument("--bucket", type=int, default=30)
    args = ap.parse_args()

    rows = [json.loads(line) for line in open(CACHE, encoding="utf-8")]
    rows = sorted((r for r in rows if r["asset"] == args.asset),
                  key=lambda r: r["start"])[-args.windows:]
    if not rows:
        raise SystemExit("no cached windows for that asset")
    records = []
    for r in rows:
        records.append("\t".join((r["up_token"], str(r["start"]), "1")))
        records.append("\t".join((r["down_token"], str(r["start"]), "0")))
    external = ExternalData(
        data=("\n".join(records) + "\n").encode(), file_name="wt",
        fmt="TabSeparated", structure="token String, start_ts UInt32, side UInt8")

    t0, t1 = rows[0]["start"] - 1800, rows[-1]["start"] + 900
    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
        settings={"max_execution_time": 900, "max_memory_usage": 8_000_000_000,
                  "max_query_size": 300_000_000})
    query = f"""
    WITH legs AS (
      SELECT multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
             toUnixTimestamp(block_timestamp) AS ts,
             toFloat64(if(maker_asset_id='0', taker_amount_filled,
                          maker_amount_filled))/1e6 AS sh,
             toFloat64(if(maker_asset_id='0', maker_amount_filled,
                          taker_amount_filled))/1e6 AS usd
      FROM trade_history
      WHERE block_timestamp >= toDateTime({t0}) AND block_timestamp < toDateTime({t1})
    )
    SELECT intDiv(l.ts - w.start_ts + 3000, {args.bucket})*{args.bucket} - 3000 AS bucket,
           w.side AS side, sum(l.sh) AS shares, sum(l.usd) AS usd, count() AS n
    FROM legs l INNER JOIN wt w ON l.token = w.token
    WHERE l.ts - w.start_ts BETWEEN -180 AND 330 AND l.sh > 0
    GROUP BY bucket, side ORDER BY bucket, side
    """
    agg: dict[int, dict[int, tuple[float, float, int]]] = {}
    for bucket, side, shares, usd, n in client.query(
            query, external_data=external).result_rows:
        agg.setdefault(bucket, {})[side] = (shares, usd, n)

    print(f"asset={args.asset} windows={len(rows)} bucket={args.bucket}s")
    print(f"{'offset s':>13} {'upPx':>7} {'dnPx':>7} {'PAIR':>8} {'shares':>11} {'trades':>9}")
    for bucket in sorted(agg):
        sides = agg[bucket]
        if 0 not in sides or 1 not in sides:
            continue
        ush, uusd, un = sides[1]
        dsh, dusd, dn = sides[0]
        pair = uusd / ush + dusd / dsh
        mark = "  <- premium" if pair >= 1.03 else ""
        print(f"{bucket:>6} to {bucket+args.bucket:<4} {uusd/ush:>7.4f} {dusd/dsh:>7.4f} "
              f"{pair:>8.4f} {ush+dsh:>11,.0f} {un+dn:>9,.0f}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
