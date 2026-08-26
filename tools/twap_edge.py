"""Is the observable part of the settling TWAP already in Polymarket's price?

tools/twap_observability.py showed the outcome is 92.9% determined with 60s
left and 95.7% with 30s left. That is only worth money if the market has not
already priced it. This simulates the obvious trade:

  at the checkpoint, read the partial TWAP signal; if it points Up, buy Up
  (else buy Down) at Polymarket, hold to settlement, collect $1 or $0.

and reports the average P&L per share after the taker fee 0.07*p*(1-p).

Deliberately OPTIMISTIC in two ways, both of which favour the strategy:
  * entry uses the last traded price, not the ask you would actually lift,
  * unlimited size at that price, with no queue and no impact.
If it does not clear zero under these assumptions it cannot clear zero live.

Read-only. Needs local ClickHouse and gamma for token resolution.
"""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import urllib.request
from collections import defaultdict

import clickhouse_connect  # type: ignore[import-untyped]


def taker_fee(price: float, shares: float = 1.0) -> float:
    return 0.07 * price * (1 - price) * shares


def load_reference(paths: list[str]) -> dict[int, dict[int, float]]:
    per: dict[int, dict[int, float]] = defaultdict(dict)
    for pattern in paths:
        for path in sorted(glob.glob(pattern)) or [pattern]:
            try:
                db = sqlite3.connect(path)
                rows = db.execute(
                    "SELECT observed_at, value_e18 FROM reference_prices "
                    "WHERE asset='btc' ORDER BY observed_at").fetchall()
            except sqlite3.Error as exc:
                # never swallow this: an unreadable db silently becomes
                # "no windows", which reads as a clean negative result
                print(f"  WARNING: cannot read {path}: {exc}")
                continue
            for observed_at, value in rows:
                ts = int(float(observed_at))
                per[ts // 300 * 300][ts - (ts // 300 * 300)] = int(value) / 1e18
    return per


def tokens_for(start: int) -> tuple[str, str] | None:
    url = f"https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{start}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        events = json.load(urllib.request.urlopen(req, timeout=15))
    except Exception:
        return None
    markets = (events[0].get("markets") if events else None) or []
    if not markets:
        return None
    ids = markets[0].get("clobTokenIds")
    if isinstance(ids, str):
        ids = json.loads(ids)
    return (ids[0], ids[1]) if ids and len(ids) >= 2 else None


# Any print, regardless of side. Optimistic: a mark you cannot
# necessarily buy at.
PRICE_SQL = """
SELECT argMax(px, ts) AS last_px FROM (
  SELECT toUnixTimestamp(block_timestamp) AS ts,
         toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))
           / nullIf(toFloat64(if(maker_asset_id='0', taker_amount_filled,
                                 maker_amount_filled)), 0) AS px
  FROM trade_history
  WHERE block_timestamp >  toDateTime({lo:UInt32})
    AND block_timestamp <= toDateTime({hi:UInt32})
    AND multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) = {tok:String}
)
WHERE px IS NOT NULL AND px > 0 AND px < 1
"""

# Only prints where a TAKER BOUGHT this token, i.e. someone lifted the ask.
# The maker gives the token and the taker gives USDC ('0'), so the executed
# price is an ask we could actually have paid - which is what buying costs.
ASK_SQL = """
SELECT argMax(px, ts) AS ask_px, argMax(sh, ts) AS ask_sh FROM (
  SELECT toUnixTimestamp(block_timestamp) AS ts,
         toFloat64(taker_amount_filled) / nullIf(toFloat64(maker_amount_filled), 0)
           AS px,
         toFloat64(maker_amount_filled) / 1e6 AS sh
  FROM trade_history
  WHERE block_timestamp >  toDateTime({lo:UInt32})
    AND block_timestamp <= toDateTime({hi:UInt32})
    AND maker_asset_id = {tok:String}
    AND taker_asset_id = '0'
)
WHERE px IS NOT NULL AND px > 0 AND px < 1
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbs", nargs="+", required=True)
    ap.add_argument("--checkpoints", nargs="+", type=int, default=[240, 270, 285])
    ap.add_argument("--min-bps", type=float, default=1.0,
                    help="ignore windows whose signal is flatter than this")
    ap.add_argument("--ask", action="store_true",
                    help="enter at a real ask print (taker-buy) instead of any print")
    ap.add_argument("--signal-lag-s", type=float, default=0.0,
                    help="seconds between a reference sample being observed and "
                         "this process receiving it. Every sample in our data "
                         "arrives late (median 1.678s), so entry must be priced "
                         "AFTER checkpoint+lag or the backtest is lookahead.")
    ap.add_argument("--fresh-s", type=int, default=0,
                    help="only use prints within this many seconds of the "
                         "checkpoint; 0 means any print since the window opened. "
                         "A stale print fabricates an entry price that was never "
                         "available at the checkpoint.")
    args = ap.parse_args()

    windows = load_reference(args.dbs)
    c = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly", settings={"max_execution_time": 600})

    def nearest(series, target, tol=4):
        for d in range(tol + 1):
            for e in (target - d, target + d):
                if e in series:
                    return series[e]
        return None

    results = {cp: [] for cp in args.checkpoints}
    resolved = skipped = 0
    for start, series in sorted(windows.items()):
        opening, final = nearest(series, 0), nearest(series, 300)
        if opening is None or final is None or opening <= 0 or final == opening:
            continue
        toks = tokens_for(start)
        if not toks:
            skipped += 1
            continue
        resolved += 1
        up_token, down_token = toks
        outcome_up = final > opening
        for cp in args.checkpoints:
            value = nearest(series, cp)
            if value is None:
                continue
            signal_bps = (value / opening - 1) * 10_000
            if abs(signal_bps) < args.min_bps:
                continue
            predict_up = signal_bps > 0
            token = up_token if predict_up else down_token
            sql = ASK_SQL if args.ask else PRICE_SQL
            if args.signal_lag_s:
                # we cannot act until the sample reaches us; price the entry
                # from prints strictly AFTER that, never before
                act_at = start + cp + args.signal_lag_s
                lo, hi = act_at, act_at + max(args.fresh_s, 5)
            else:
                lo = start + cp - args.fresh_s if args.fresh_s else start
                hi = start + cp
            rows = c.query(sql, parameters={
                "lo": int(lo), "hi": int(hi), "tok": token}).result_rows
            price = rows[0][0] if rows and rows[0][0] else None
            if price is None:
                continue
            size = float(rows[0][1]) if args.ask and len(rows[0]) > 1 else 0.0
            won = (predict_up == outcome_up)
            pnl = (1.0 if won else 0.0) - price - taker_fee(price)
            results[cp].append((pnl, price, won, size))

    print(f"windows resolved: {resolved}   token lookup failed: {skipped}\n")
    print(f"{'checkpoint':<16}{'n':>5}{'hit%':>8}{'avg entry':>11}"
          f"{'avg P&L/share':>15}{'total':>10}{'med size':>12}")
    for cp in args.checkpoints:
        rows = results[cp]
        if not rows:
            continue
        n = len(rows)
        hit = sum(1 for r in rows if r[2]) / n
        entry = sum(r[1] for r in rows) / n
        avg = sum(r[0] for r in rows) / n
        sizes = sorted(r[3] for r in rows if r[3] > 0)
        med_sz = sizes[len(sizes)//2] if sizes else 0.0
        print(f"T+{cp} ({300-cp:>2}s left){n:>5}{hit:>7.1%}{entry:>11.3f}"
              f"{avg:>+15.4f}{sum(r[0] for r in rows):>+10.2f}"
              f"{med_sz:>10.1f}sh")

    print("\nEntry uses the last TRADED price and assumes unlimited size with no")
    print("queue or impact, so these numbers are an upper bound on what is real.")


if __name__ == "__main__":
    main()
