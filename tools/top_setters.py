#!/usr/bin/env python3
"""Rank wallets on official resolved 5-minute crypto market windows.

Unlike the old implementation, this groups by market slug, includes the full
token lifecycle (markets currently open about 24 hours before their event),
computes inventory deficits per token, reports direct CTF events separately,
and never mutates ClickHouse. Start/end are inclusive window timestamps.

Examples:
  python tools/top_setters.py --hours 24
  python tools/top_setters.py --start 2026-08-24T17:15:00Z \
      --end 2026-08-24T22:25:00Z
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import sys
import time
from typing import Sequence

import clickhouse_connect

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.clickhouse_forensics import fetch_direct_ctf, fetch_token_activity
from tools.market_windows import ASSET_PREFIX, resolve_windows
from tools.wallet_metrics import summarize_wallets


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_CACHE = "backtest_cache/set_forensics_windows.jsonl"


def parse_timestamp(value: str) -> int:
    if value.isdigit():
        result = int(value)
    else:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        result = int(parsed.timestamp())
    if result % 300:
        raise argparse.ArgumentTypeError("timestamp must be 5-minute aligned")
    return result


def iso(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--start", type=parse_timestamp)
    parser.add_argument("--end", type=parse_timestamp)
    parser.add_argument("--assets", default="btc,eth,sol,xrp")
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--min-windows", type=int, default=20)
    parser.add_argument("--min-both", type=float, default=50.0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json-output")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    assets = [part.strip().lower() for part in args.assets.split(",") if part.strip()]
    unknown = sorted(set(assets) - set(ASSET_PREFIX))
    if not assets or unknown:
        raise SystemExit(f"invalid assets: {unknown or assets}")
    if (args.start is None) != (args.end is None):
        raise SystemExit("--start and --end must be supplied together")
    if args.start is None:
        end = (int(time.time()) // 300) * 300 - 900
        periods = max(1, math.ceil(args.hours * 12))
        start = end - (periods - 1) * 300
    else:
        start, end = args.start, args.end
    if end < start:
        raise SystemExit("--end must not be before --start")

    windows, missing = resolve_windows(
        assets, start, end, args.cache, max(1, args.workers),
        fetch_missing=not args.no_fetch, allow_missing=args.allow_missing,
    )
    if not windows:
        raise SystemExit("no resolved windows in requested period")
    expected = len(assets) * (((end - start) // 300) + 1)
    counts = {asset: sum(window.asset == asset for window in windows) for asset in assets}
    print(f"period: {iso(start)} .. {iso(end)} inclusive")
    print(f"resolved asset-windows: {len(windows)} / {expected} | "
          + " ".join(f"{asset}={counts[asset]}" for asset in assets)
          + (f" | missing={len(missing)}" if missing else ""))

    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
    )
    summaries = summarize_wallets(
        fetch_token_activity(client, windows), fetch_direct_ctf(client, windows)
    )
    selected = [row for row in summaries
                if row.market_windows >= args.min_windows and row.both_pct >= args.min_both][:args.limit]

    print("inventory-floor is unexplained sell inventory, not proof of minting")
    print("direct CTF values match the trading address only; proxies/transfers remain separate")
    print("buySum/sellSum are matched-share average price-sum proxies, not ordered cycle matches")
    print(f"{'wallet':<44}{'pnl$':>10}{'vol$':>11}{'mkts':>6}{'both':>7}"
          f"{'buy2':>7}{'sell2':>7}{'buySum':>8}{'sellSum':>9}"
          f"{'invfloor':>10}{'maker':>7}{'split$':>10}{'merge$':>10}")
    for row in selected:
        buy_sum = f"{row.buy_pair_sum:.3f}" if row.buy_pair_sum is not None else "-"
        sell_sum = f"{row.sell_pair_sum:.3f}" if row.sell_pair_sum is not None else "-"
        print(f"{row.wallet:<44}{row.pnl:>+10,.0f}{row.volume:>11,.0f}{row.market_windows:>6}"
              f"{row.both_pct:>6.1f}%{row.bought_both_pct:>6.1f}%{row.sold_both_pct:>6.1f}%"
              f"{buy_sum:>8}{sell_sum:>9}"
              f"{row.inventory_floor_pct:>9.1f}%{row.maker_share_pct:>6.1f}%"
              f"{row.direct_split_sets:>10,.0f}{row.direct_merge_sets:>10,.0f}")
    if not selected:
        print("(no wallets met the filters)")

    if args.json_output:
        target = pathlib.Path(args.json_output)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "period": {"start": start, "end": end, "inclusive": True},
            "assets": assets,
            "resolved_asset_windows": len(windows),
            "missing": missing,
            "wallets": [row.as_dict() for row in selected],
        }
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
