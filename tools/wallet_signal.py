#!/usr/bin/env python3
"""Measure whether wallet trade-flow inventory leans toward eventual winners."""

from __future__ import annotations

import argparse
import dataclasses
import math
import pathlib
import re
import sys
from collections import defaultdict
from typing import Sequence

import clickhouse_connect  # type: ignore[import-untyped]

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.clickhouse_forensics import (
    LIFECYCLE_LOOKBACK_S,
    LIFECYCLE_TAIL_S,
    SETTINGS,
    _legs_sql,
    window_external_data,
)
from tools.market_windows import ASSET_PREFIX, resolve_windows
from tools.top_setters import DEFAULT_CACHE, iso, parse_timestamp


WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclasses.dataclass(frozen=True)
class Fill:
    wallet: str
    slug: str
    start: int
    winner_up: bool
    side_up: bool
    ts: int
    cash: float
    net_shares: float
    volume: float
    is_maker: bool
    taker_fee: float = 0.0


@dataclasses.dataclass(frozen=True)
class SignalSummary:
    markets: int
    calls: int
    hits: int
    hit_low: float
    hit_high: float
    weighted_alignment: float
    directional_luck: float
    neutral_pnl: float
    actual_pnl: float
    volume: float


def _wilson(hits: int, calls: int) -> tuple[float, float]:
    if not calls:
        return 0.0, 0.0
    z = 1.959963984540054
    rate = hits / calls
    scale = 1 + z * z / calls
    center = (rate + z * z / (2 * calls)) / scale
    radius = z * math.sqrt(rate * (1 - rate) / calls + z * z / (4 * calls**2)) / scale
    return center - radius, center + radius


def summarize_signal(
    fills: Sequence[Fill], wallet: str, cutoff_s: int, min_call_shares: float = 5.0,
) -> SignalSummary:
    markets: dict[str, list[Fill]] = defaultdict(list)
    for fill in fills:
        if fill.wallet == wallet and fill.ts < fill.start + cutoff_s:
            markets[fill.slug].append(fill)
    calls = hits = 0
    aligned_total = absolute_total = neutral = actual = volume = 0.0
    for rows in markets.values():
        winner_up = rows[0].winner_up
        if any(row.winner_up != winner_up or row.start != rows[0].start for row in rows):
            raise ValueError(f"inconsistent window metadata for {rows[0].slug}")
        up = sum(row.net_shares for row in rows if row.side_up)
        down = sum(row.net_shares for row in rows if not row.side_up)
        cash = sum(row.cash - row.taker_fee for row in rows)
        volume += sum(row.volume for row in rows)
        direction = up - down
        winner_sign = 1 if winner_up else -1
        aligned = winner_sign * direction
        aligned_total += aligned
        absolute_total += abs(direction)
        neutral += cash + 0.5 * (up + down)
        actual += cash + (up if winner_up else down)
        if abs(direction) >= min_call_shares:
            calls += 1
            hits += int(aligned > 0)
    low, high = _wilson(hits, calls)
    return SignalSummary(
        len(markets), calls, hits, low, high,
        aligned_total / absolute_total if absolute_total else 0.0,
        0.5 * aligned_total, neutral, actual, volume,
    )


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_timestamp, required=True)
    parser.add_argument("--end", type=parse_timestamp, required=True)
    parser.add_argument("--wallet", action="append", required=True)
    parser.add_argument("--assets", default="btc,eth,sol,xrp")
    parser.add_argument("--cutoffs", default="0,30,60,150,240,300")
    parser.add_argument("--min-call-shares", type=float, default=5.0)
    parser.add_argument("--flow", choices=("all", "maker", "taker"), default="all")
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    return parser.parse_args(argv)


def fetch_fills(client, windows, wallets: Sequence[str]) -> list[Fill]:
    t0 = min(window.start for window in windows) - LIFECYCLE_LOOKBACK_S
    t1 = max(window.start for window in windows) + LIFECYCLE_TAIL_S
    selected = ",".join(f"'{wallet}'" for wallet in wallets)
    query = f"""
    SELECT lower(l.wallet), w.slug, w.start_ts, w.side=toUInt8(w.payoff), w.side=1,
           l.ts, l.cash, l.net_shares, l.usdc, l.is_maker, l.taker_fee
    FROM ({_legs_sql(t0, t1)}) l
    INNER JOIN set_windows w ON l.token=w.token
    WHERE lower(l.wallet) IN ({selected})
    ORDER BY lower(l.wallet), w.slug, l.ts, l.block_number, l.log_index
    """
    rows = client.query(
        query, settings=SETTINGS, external_data=window_external_data(windows)
    ).result_rows
    return [Fill(
        str(row[0]), str(row[1]), int(row[2]), bool(row[3]), bool(row[4]),
        int(row[5]), float(row[6]), float(row[7]), float(row[8]), bool(row[9]),
        float(row[10]),
    ) for row in rows]


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    wallets = sorted({wallet.lower() for wallet in args.wallet})
    if any(not WALLET_RE.fullmatch(wallet) for wallet in wallets):
        raise SystemExit("every --wallet must be a 0x-prefixed 20-byte address")
    assets = [value.strip().lower() for value in args.assets.split(",") if value.strip()]
    cutoffs = sorted({int(value) for value in args.cutoffs.split(",")})
    if (not assets or set(assets) - set(ASSET_PREFIX) or args.end < args.start
            or not cutoffs or min(cutoffs) < 0 or max(cutoffs) > 300
            or args.min_call_shares < 0):
        raise SystemExit("invalid assets, period, cutoffs, or call threshold")
    windows, _ = resolve_windows(
        assets, args.start, args.end, args.cache,
        fetch_missing=False, allow_missing=False,
    )
    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
    )
    fills = fetch_fills(client, windows, wallets)
    if args.flow != "all":
        maker = args.flow == "maker"
        fills = [fill for fill in fills if fill.is_maker == maker]
    print(f"period: {iso(args.start)} .. {iso(args.end)} | resolved={len(windows)} "
          f"| flow={args.flow} | call>={args.min_call_shares:g} shares")
    print("dir$ = terminal outcome PnL minus 50/50 neutral PnL for fills before cutoff")
    print("trade-flow inventory omits transfers; alignment is evidence, not order intent")
    print(f"{'wallet':<44}{'t':>5}{'mkts':>6}{'calls':>7}{'hit% [95% CI]':>21}"
          f"{'align%':>9}{'dir$':>11}{'neutral$':>11}{'actual$':>11}{'vol$':>11}")
    for wallet in wallets:
        for cutoff in cutoffs:
            summary = summarize_signal(fills, wallet, cutoff, args.min_call_shares)
            hit = 100 * summary.hits / summary.calls if summary.calls else 0.0
            interval = f"{hit:4.1f} [{100*summary.hit_low:4.1f},{100*summary.hit_high:4.1f}]"
            print(f"{wallet:<44}{cutoff:>4}s{summary.markets:>6}{summary.calls:>7}"
                  f"{interval:>21}{100*summary.weighted_alignment:>8.1f}%"
                  f"{summary.directional_luck:>11,.0f}{summary.neutral_pnl:>11,.0f}"
                  f"{summary.actual_pnl:>11,.0f}{summary.volume:>11,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
