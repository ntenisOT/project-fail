#!/usr/bin/env python3
"""Break wallet terminal markout down by time, role, action, and price."""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys
from collections import defaultdict
from typing import Sequence

import clickhouse_connect  # type: ignore[import-untyped]

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.market_windows import ASSET_PREFIX, resolve_windows
from tools.top_setters import DEFAULT_CACHE, iso, parse_timestamp
from tools.wallet_signal import Fill, WALLET_RE, fetch_fills


TIME_BINS = ((-10**9, 0, "pre"), (0, 60, "0-60"), (60, 150, "60-150"),
             (150, 240, "150-240"), (240, 300, "240-300"),
             (300, 10**9, "post"))
PRICE_BINS = ((0, .10), (.10, .25), (.25, .50), (.50, .75), (.75, .90), (.90, 1.01))


@dataclasses.dataclass
class Markout:
    fills: int = 0
    shares: float = 0.0
    volume: float = 0.0
    gross_edge: float = 0.0
    taker_fee: float = 0.0

    @property
    def net_edge(self) -> float:
        return self.gross_edge - self.taker_fee


def _add(target: Markout, fill: Fill) -> None:
    shares = abs(fill.net_shares)
    payoff = float(fill.side_up == fill.winner_up)
    target.fills += 1
    target.shares += shares
    target.volume += fill.volume
    target.gross_edge += fill.cash + fill.net_shares * payoff
    target.taker_fee += fill.taker_fee


def summarize_time(fills: Sequence[Fill], wallet: str) -> dict[tuple[str, str], Markout]:
    result: dict[tuple[str, str], Markout] = defaultdict(Markout)
    for fill in fills:
        if fill.wallet != wallet or not fill.net_shares:
            continue
        elapsed = fill.ts - fill.start
        time_bin = next(label for low, high, label in TIME_BINS if low <= elapsed < high)
        _add(result[(time_bin, "maker" if fill.is_maker else "taker")], fill)
    return result


def summarize_price(
    fills: Sequence[Fill], wallet: str, from_second: int, to_second: int,
) -> dict[tuple[str, str, str], Markout]:
    result: dict[tuple[str, str, str], Markout] = defaultdict(Markout)
    for fill in fills:
        elapsed = fill.ts - fill.start
        if (fill.wallet != wallet or not fill.net_shares
                or not from_second <= elapsed < to_second):
            continue
        price = fill.volume / abs(fill.net_shares)
        band = next(f"{low:.2f}-{high:.2f}" for low, high in PRICE_BINS
                    if low <= price < high)
        action = "buy" if fill.net_shares > 0 else "sell"
        _add(result[(action, "maker" if fill.is_maker else "taker", band)], fill)
    return result


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_timestamp, required=True)
    parser.add_argument("--end", type=parse_timestamp, required=True)
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--assets", default="btc,eth,sol,xrp")
    parser.add_argument("--from-second", type=int, default=0)
    parser.add_argument("--to-second", type=int, default=300)
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    return parser.parse_args(argv)


def _print_row(label: str, row: Markout) -> None:
    cents = 100 * row.net_edge / row.shares if row.shares else 0.0
    print(f"{label:<25}{row.fills:>8}{row.shares:>11,.0f}{row.volume:>12,.0f}"
          f"{row.gross_edge:>11,.0f}{row.taker_fee:>9,.0f}"
          f"{row.net_edge:>11,.0f}{cents:>9.2f}")


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    wallet = args.wallet.lower()
    assets = [value.strip().lower() for value in args.assets.split(",") if value.strip()]
    if (not WALLET_RE.fullmatch(wallet) or not assets or set(assets) - set(ASSET_PREFIX)
            or args.end < args.start or args.from_second >= args.to_second):
        raise SystemExit("invalid wallet, assets, period, or segment")
    windows, _ = resolve_windows(
        assets, args.start, args.end, args.cache,
        fetch_missing=False, allow_missing=False,
    )
    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
    )
    fills = fetch_fills(client, windows, [wallet])
    print(f"period: {iso(args.start)} .. {iso(args.end)} | resolved={len(windows)}")
    print("net$ subtracts explicit taker fee; maker rebates and transfers are excluded")
    print(f"{'bucket':<25}{'fills':>8}{'shares':>11}{'volume$':>12}"
          f"{'gross$':>11}{'fee$':>9}{'net$':>11}{'c/share':>9}")
    by_time = summarize_time(fills, wallet)
    for _, _, label in TIME_BINS:
        for role in ("maker", "taker"):
            row = by_time.get((label, role))
            if row is not None:
                _print_row(f"{label} {role}", row)
    print(f"\naction/price markout for elapsed [{args.from_second},{args.to_second}) seconds")
    by_price = summarize_price(fills, wallet, args.from_second, args.to_second)
    for key in sorted(by_price):
        _print_row(" ".join(key), by_price[key])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
