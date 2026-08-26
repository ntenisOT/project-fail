#!/usr/bin/env python3
"""Fast tape backtests of two-sided pair strategies over many windows.

Motivation: a live paper arm needs days to produce a handful of scored
windows. The public trade tape gives 600 windows in minutes, so naive
strategy shapes can be falsified before paying for an engine change.

Two shapes are tested, both queue-OPTIMISTIC (front of queue, fill on any
qualifying print), so real results are worse, never better:

  preopen : rest bids on both outcomes in T-120..T-1, merge matched pairs at
            $1.00, let the imbalance ride to settlement.
  latesell: mint sets at $1.00 and rest asks on both outcomes from t_start,
            merge unsold matched leftovers back at $1.00.

Measured 2026-08-26 on 600 BTC windows:
  preopen, static symmetric bids      : -$4.21 to -$11.03 per window
  preopen, book-anchored + balance cap: -$0.47 to +$0.00 per window
  latesell from T+180/210/240         : -$1.60 to -$2.81 per window, 13-27% win

Conclusion: the pair-cost curve measured by tools/pair_cost_curve.py is real
but is NOT capturable by a symmetric two-sided maker. Pre-open cheapness is
adverse selection (you fill the side that just moved against you) and the
late premium is volume-weighted onto the winning side, which a balanced
seller cannot realise.

Usage: python tools/tape_backtest.py [--shape preopen|latesell]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import pickle

import clickhouse_connect
from clickhouse_connect.driver.external import ExternalData

CACHE = "backtest_cache/set_forensics_windows.jsonl"


def load_tape(asset: str, windows: int, lo: int, hi: int, cache_path: str):
    if os.path.exists(cache_path):
        return pickle.load(open(cache_path, "rb"))
    rows = [json.loads(line) for line in open(CACHE, encoding="utf-8")]
    rows = sorted((r for r in rows if r["asset"] == asset),
                  key=lambda r: r["start"])[-windows:]
    recs = []
    for r in rows:
        recs.append("\t".join((r["up_token"], str(r["start"]), "1", str(r["winner_up"]))))
        recs.append("\t".join((r["down_token"], str(r["start"]), "0", str(r["winner_up"]))))
    external = ExternalData(
        data=("\n".join(recs) + "\n").encode(), file_name="wt", fmt="TabSeparated",
        structure="token String, start_ts UInt32, side UInt8, winner_up UInt8")
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
      WHERE block_timestamp >= toDateTime({t0}) AND block_timestamp < toDateTime({t1}))
    SELECT w.start_ts, w.side, w.winner_up, l.ts - w.start_ts AS off, l.sh, l.usd/l.sh
    FROM legs l INNER JOIN wt w ON l.token = w.token
    WHERE l.ts - w.start_ts BETWEEN {lo} AND {hi} AND l.sh > 0
    ORDER BY w.start_ts, off"""
    tape: dict = collections.defaultdict(list)
    for start, side, win, off, sh, px in client.query(
            query, external_data=external).result_rows:
        tape[(start, win)].append((off, side, sh, px))
    tape = dict(tape)
    pickle.dump(tape, open(cache_path, "wb"))
    return tape


def preopen(tape, delta: float, tol: float, cap=200.0, clip=6.0):
    total = both = 0.0
    for (_start, win), trades in tape.items():
        got = {1: 0.0, 0: 0.0}
        cost = {1: 0.0, 0: 0.0}
        last = {1: None, 0: None}
        for _off, side, sh, px in trades:
            other = 1 - side
            bid = None if last[side] is None else round(last[side] - delta, 2)
            if (bid is not None and px <= bid and got[side] < cap
                    and got[side] <= got[other] + tol):
                take = min(sh, clip, cap - got[side])
                got[side] += take
                cost[side] += take * bid
            last[side] = px
        matched = min(got[1], got[0])
        excess = abs(got[1] - got[0])
        excess_side = 1 if got[1] > got[0] else 0
        total += (matched + excess * (1.0 if (win == 1) == (excess_side == 1) else 0.0)
                  - cost[1] - cost[0])
        both += got[1] > 0 and got[0] > 0
    return total / len(tape), both


def latesell(tape, t_start: float, delta: float, tol: float, cap=200.0, clip=6.0):
    total = both = 0.0
    for (_start, win), trades in tape.items():
        got = {1: 0.0, 0: 0.0}
        rev = {1: 0.0, 0: 0.0}
        last = {1: None, 0: None}
        for off, side, sh, px in trades:
            other = 1 - side
            if off >= t_start and last[side] is not None:
                ask = round(last[side] + delta, 2)
                if px >= ask and got[side] < cap and got[side] <= got[other] + tol:
                    take = min(sh, clip, cap - got[side])
                    got[side] += take
                    rev[side] += take * ask
            last[side] = px
        minted = max(got[1], got[0])
        excess = abs(got[1] - got[0])
        excess_side = 0 if got[1] > got[0] else 1
        total += (rev[1] + rev[0] - minted
                  + excess * (1.0 if (win == 1) == (excess_side == 1) else 0.0))
        both += got[1] > 0 and got[0] > 0
    return total / len(tape), both


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shape", choices=("preopen", "latesell"), default="preopen")
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--windows", type=int, default=600)
    args = ap.parse_args()
    if args.shape == "preopen":
        tape = load_tape(args.asset, args.windows, -120, -1, "/tmp/preopen_tape.pkl")
        print(f"windows={len(tape)} shape=preopen (queue-optimistic)")
        print(f"{'delta':>6} {'tol':>5} {'$/win':>9} {'both':>6}")
        for delta in (0.01, 0.02, 0.03):
            for tol in (7.0, 20.0):
                per, both = preopen(tape, delta, tol)
                print(f"{delta:>6.2f} {tol:>5.0f} {per:>9.3f} {both:>6.0f}")
    else:
        tape = load_tape(args.asset, args.windows, 0, 300, "/tmp/inwin_tape.pkl")
        print(f"windows={len(tape)} shape=latesell (queue-optimistic)")
        print(f"{'from':>5} {'delta':>6} {'$/win':>9} {'both':>6}")
        for t in (180, 210, 240):
            for delta in (0.01, 0.02, 0.03):
                per, both = latesell(tape, t, delta, 7.0)
                print(f"{t:>5} {delta:>6.2f} {per:>9.3f} {both:>6.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
