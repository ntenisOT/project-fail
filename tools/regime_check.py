"""Is there still organic flow in the 5-minute crypto windows?

The Qwen review's top-ranked concern was that every strategy result we have is
from Aug 18-25, inside the August liquidity-reward programme, and that the 5m
products since went to ~0 trades/window with books still quoted - a subsidy
shape with the organic flow gone. If true, no result measured in that period
transfers, and sizing capital off Gen88 economics is sizing off a dead regime.

markets_meta cannot answer this (405 stale rows, no updown slugs), so this
resolves each window's tokens from gamma and counts real fills in ClickHouse.

Read-only. Compares like-for-like clock windows across days.
"""
from __future__ import annotations

import argparse
import json
import urllib.request

import clickhouse_connect  # type: ignore[import-untyped]

GAMMA = "https://gamma-api.polymarket.com/events?slug={slug}"


def tokens_for(asset: str, start: int) -> list[str]:
    slug = f"{asset}-updown-5m-{start}"
    req = urllib.request.Request(GAMMA.format(slug=slug),
                                 headers={"User-Agent": "Mozilla/5.0"})
    try:
        events = json.load(urllib.request.urlopen(req, timeout=15))
    except Exception:
        return []
    if not events:
        return []
    markets = events[0].get("markets") or []
    if not markets:
        return []
    ids = markets[0].get("clobTokenIds")
    if isinstance(ids, str):
        ids = json.loads(ids)
    return list(ids or [])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--windows", type=int, default=10,
                    help="consecutive 5m windows to sample per day")
    ap.add_argument("--start-utc", type=int, default=11,
                    help="hour of day (UTC) to sample from, same for every day")
    ap.add_argument("--days", nargs="+", required=True,
                    help="epoch seconds of 00:00 UTC for each day to compare")
    args = ap.parse_args()

    c = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly", settings={"max_execution_time": 900})

    print(f"{args.asset.upper()} 5-minute windows | {args.windows} consecutive "
          f"windows from {args.start_utc:02d}:00 UTC each day\n")
    print(f"{'day (UTC)':<14}{'windows':>9}{'resolved':>10}{'trades':>10}"
          f"{'trades/window':>15}")

    for day_epoch in (int(d) for d in args.days):
        base = day_epoch + args.start_utc * 3600
        starts = [base + i * 300 for i in range(args.windows)]
        found, total = 0, 0
        for start in starts:
            toks = tokens_for(args.asset, start)
            if not toks:
                continue
            found += 1
            rows = c.query(
                "SELECT count() FROM trade_history "
                "WHERE block_timestamp >= toDateTime({a:UInt32}) "
                "  AND block_timestamp <  toDateTime({b:UInt32}) "
                "  AND (maker_asset_id IN {t:Array(String)} "
                "    OR taker_asset_id IN {t:Array(String)})",
                parameters={"a": start - 300, "b": start + 360, "t": toks},
            ).result_rows
            total += int(rows[0][0]) if rows else 0
        import datetime
        label = datetime.datetime.utcfromtimestamp(day_epoch).strftime("%Y-%m-%d")
        per = (total / found) if found else 0.0
        print(f"{label:<14}{args.windows:>9}{found:>10}{total:>10}{per:>15.1f}")

    print("\nresolved = windows gamma still returns tokens for; a low count means")
    print("the market metadata aged out, not that flow stopped.")


if __name__ == "__main__":
    main()
