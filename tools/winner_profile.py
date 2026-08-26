#!/usr/bin/env python3
"""Reverse-engineer one winner wallet's mint-to-make behaviour from public fills.

Full-cycle accounting insight: if the wallet mints N complete sets at $1.00,
sells `a` Up and `b` Down as maker, merges the matched leftovers back at $1.00
and lets the imbalance ride to settlement, then N CANCELS OUT:

    PnL = up_usd + dn_usd - max(a, b) + |a - b| * [side sold less of wins]

So per-window economics are recoverable from public fills alone, without
knowing the mint size. Rebates and liquidity rewards are excluded (upside).

Usage: python tools/winner_profile.py [--wallet 0x..] [--windows 400]
"""
from __future__ import annotations

import argparse
import collections
import json

import clickhouse_connect

REFERENCE = "0x1Dd2A69e73BA444ecd5D87f0073d51a670ad51c2"
CACHE = "backtest_cache/set_forensics_windows.jsonl"


def pct(values, q):
    return sorted(values)[int(len(values) * q)] if values else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", default=REFERENCE)
    ap.add_argument("--windows", type=int, default=400)
    ap.add_argument("--asset", default="btc")
    args = ap.parse_args()

    rows = [json.loads(line) for line in open(CACHE, encoding="utf-8")]
    rows = sorted((r for r in rows if r["asset"] == args.asset),
                  key=lambda r: r["start"])[-args.windows:]
    tokens = {}
    for r in rows:
        tokens[r["up_token"]] = (r["slug"], r["start"], "Up", r["winner_up"])
        tokens[r["down_token"]] = (r["slug"], r["start"], "Down", r["winner_up"])

    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly", settings={"max_execution_time": 900})
    joined = "','".join(tokens)
    fills = client.query(
        f"SELECT maker_asset_id, toUnixTimestamp(block_timestamp),"
        f" toFloat64(maker_amount_filled)/1e6, toFloat64(taker_amount_filled)/1e6,"
        f" toFloat64(fee)/1e6 FROM trade_history"
        f" WHERE lower(maker)=lower('{args.wallet}') AND taker_asset_id='0'"
        f" AND maker_asset_id IN ('{joined}')").result_rows

    win = collections.defaultdict(
        lambda: {"Up": [0.0, 0.0], "Down": [0.0, 0.0], "win": None, "offs": []})
    fee_total = 0.0
    for token, ts, shares, usd, fee in fills:
        slug, start, side, winner = tokens[token]
        entry = win[slug]
        entry[side][0] += shares
        entry[side][1] += usd
        entry["win"] = winner
        entry["offs"].append(ts - start)
        fee_total += fee

    total = paired = residual = 0.0
    pnls, firsts, lasts, imbalance, sums = [], [], [], [], []
    for slug, d in win.items():
        a, up_usd = d["Up"]
        b, dn_usd = d["Down"]
        if a <= 0 or b <= 0:
            continue
        matched = min(a, b)
        pair = up_usd * (matched / a) + dn_usd * (matched / b) - matched
        excess = abs(a - b)
        pays = (d["win"] == 0) if a > b else (d["win"] == 1)
        excess_rev = (up_usd * ((a - matched) / a) if a > b
                      else dn_usd * ((b - matched) / b))
        res = excess_rev - excess + (excess if pays else 0.0)
        pnls.append(pair + res)
        total += pair + res
        paired += pair
        residual += res
        firsts.append(min(d["offs"]))
        lasts.append(max(d["offs"]))
        imbalance.append(excess)
        sums.append(up_usd / a + dn_usd / b)

    n = len(pnls)
    print(f"wallet {args.wallet}  asset={args.asset}  windows={n}  fills={len(fills)}")
    print(f"TOTAL ${total:+,.2f}  mean/window ${total/n:+.3f}  "
          f"win-rate {100*sum(p>0 for p in pnls)/n:.1f}%")
    print(f"  paired surplus ${paired:+,.2f}   residue ${residual:+,.2f}   "
          f"taker fees ${fee_total:.2f}")
    print(f"  pair sum p25={pct(sums,.25):.4f} p50={pct(sums,.5):.4f} "
          f"p75={pct(sums,.75):.4f} mean={sum(sums)/n:.4f}")
    print(f"  first sell s p50={pct(firsts,.5):.0f}  last sell s p50={pct(lasts,.5):.0f}")
    print(f"  imbalance shares p50={pct(imbalance,.5):.1f} p90={pct(imbalance,.9):.1f}")
    print(f"  window PnL p10={pct(pnls,.1):+.2f} p50={pct(pnls,.5):+.2f} "
          f"p90={pct(pnls,.9):+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
