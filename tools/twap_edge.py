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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbs", nargs="+", required=True)
    ap.add_argument("--checkpoints", nargs="+", type=int, default=[240, 270, 285])
    ap.add_argument("--min-bps", type=float, default=1.0,
                    help="ignore windows whose signal is flatter than this")
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
            rows = c.query(PRICE_SQL, parameters={
                "lo": start, "hi": start + cp, "tok": token}).result_rows
            price = rows[0][0] if rows and rows[0][0] else None
            if price is None:
                continue
            won = (predict_up == outcome_up)
            pnl = (1.0 if won else 0.0) - price - taker_fee(price)
            results[cp].append((pnl, price, won))

    print(f"windows resolved: {resolved}   token lookup failed: {skipped}\n")
    print(f"{'checkpoint':<16}{'n':>5}{'hit%':>8}{'avg entry':>11}"
          f"{'avg P&L/share':>15}{'total':>10}")
    for cp in args.checkpoints:
        rows = results[cp]
        if not rows:
            continue
        n = len(rows)
        hit = sum(1 for _, _, w in rows if w) / n
        entry = sum(p for _, p, _ in rows) / n
        avg = sum(x for x, _, _ in rows) / n
        print(f"T+{cp} ({300-cp:>2}s left){n:>5}{hit:>7.1%}{entry:>11.3f}"
              f"{avg:>+15.4f}{sum(x for x,_,_ in rows):>+10.2f}")

    print("\nEntry uses the last TRADED price and assumes unlimited size with no")
    print("queue or impact, so these numbers are an upper bound on what is real.")


if __name__ == "__main__":
    main()
