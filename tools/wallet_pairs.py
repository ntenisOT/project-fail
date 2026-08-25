#!/usr/bin/env python3
"""Measure exact FIFO opposite-token acquisitions for selected wallets."""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys
from collections import defaultdict, deque
from typing import Sequence

import clickhouse_connect  # type: ignore[import-untyped]

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.clickhouse_forensics import SETTINGS, _legs_sql, window_external_data
from tools.market_windows import ASSET_PREFIX, resolve_windows
from tools.top_setters import DEFAULT_CACHE, iso, parse_timestamp
from tools.wallet_timing import WALLET_RE


@dataclasses.dataclass(frozen=True)
class BuyFill:
    wallet: str
    slug: str
    side: int
    order: tuple[int, int]
    ts: int
    shares: float
    price: float
    is_maker: bool
    taker_fee: float = 0.0

    @property
    def net_price(self) -> float:
        return self.price + (self.taker_fee / self.shares if self.shares else 0.0)


@dataclasses.dataclass
class _Lot:
    ts: int
    shares: float
    price: float
    is_maker: bool


@dataclasses.dataclass(frozen=True)
class _Pair:
    shares: float
    pair_sum: float
    delay_s: int
    both_maker: bool


@dataclasses.dataclass(frozen=True)
class PairSummary:
    wallet: str
    markets: int
    pair_markets: int
    bought_shares: float
    paired_shares: float
    paired_value: float
    both_maker_shares: float
    under_98_shares: float
    under_99_shares: float
    under_100_shares: float
    residual_shares: float
    median_delay_s: float
    p90_delay_s: float

    @property
    def average_sum(self) -> float | None:
        return self.paired_value / self.paired_shares if self.paired_shares else None

    def percent(self, shares: float) -> float:
        return 100 * shares / self.paired_shares if self.paired_shares else 0.0

    @property
    def completion_pct(self) -> float:
        return 200 * self.paired_shares / self.bought_shares if self.bought_shares else 0.0


def _weighted_quantile(values: list[tuple[float, float]], fraction: float) -> float:
    target = sum(weight for _, weight in values) * fraction
    cumulative = 0.0
    for value, weight in sorted(values):
        cumulative += weight
        if cumulative >= target:
            return value
    return 0.0


def summarize_pairs(fills: list[BuyFill], wallet: str) -> PairSummary:
    markets: dict[str, list[BuyFill]] = defaultdict(list)
    for fill in fills:
        if fill.wallet == wallet:
            markets[fill.slug].append(fill)
    pairs: list[_Pair] = []
    bought = residual = 0.0
    pair_markets = 0
    for rows in markets.values():
        lots: dict[int, deque[_Lot]] = {0: deque(), 1: deque()}
        market_pairs = 0.0
        for fill in sorted(rows, key=lambda row: row.order):
            bought += fill.shares
            remaining = fill.shares
            opposite = lots[1 - fill.side]
            while remaining > 1e-9 and opposite:
                lot = opposite[0]
                matched = min(remaining, lot.shares)
                pairs.append(_Pair(
                    matched, fill.net_price + lot.price,
                    max(0, fill.ts - lot.ts), fill.is_maker and lot.is_maker,
                ))
                remaining -= matched
                lot.shares -= matched
                market_pairs += matched
                if lot.shares <= 1e-9:
                    opposite.popleft()
            if remaining > 1e-9:
                lots[fill.side].append(
                    _Lot(fill.ts, remaining, fill.net_price, fill.is_maker)
                )
        residual += sum(lot.shares for side in lots.values() for lot in side)
        pair_markets += int(market_pairs > 0)
    shares = sum(pair.shares for pair in pairs)
    delays = [(float(pair.delay_s), pair.shares) for pair in pairs]
    return PairSummary(
        wallet, len(markets), pair_markets, bought, shares,
        sum(pair.shares * pair.pair_sum for pair in pairs),
        sum(pair.shares for pair in pairs if pair.both_maker),
        sum(pair.shares for pair in pairs if pair.pair_sum <= 0.98 + 1e-9),
        sum(pair.shares for pair in pairs if pair.pair_sum <= 0.99 + 1e-9),
        sum(pair.shares for pair in pairs if pair.pair_sum <= 1.0 + 1e-9),
        residual,
        _weighted_quantile(delays, 0.5) if delays else 0.0,
        _weighted_quantile(delays, 0.9) if delays else 0.0,
    )


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_timestamp, required=True)
    parser.add_argument("--end", type=parse_timestamp, required=True)
    parser.add_argument("--wallet", action="append", required=True)
    parser.add_argument("--assets", default="btc,eth,sol,xrp")
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    wallets = sorted({wallet.lower() for wallet in args.wallet})
    assets = [value.strip().lower() for value in args.assets.split(",") if value.strip()]
    if (any(not WALLET_RE.fullmatch(wallet) for wallet in wallets)
            or not assets or set(assets) - set(ASSET_PREFIX) or args.end < args.start):
        raise SystemExit("invalid wallet, assets, or period")
    windows, _ = resolve_windows(
        assets, args.start, args.end, args.cache,
        fetch_missing=False, allow_missing=False,
    )
    t0 = min(window.start for window in windows) - 26 * 60 * 60
    t1 = max(window.start for window in windows) + 60 * 60
    selected = ",".join(f"'{wallet}'" for wallet in wallets)
    query = f"""
    SELECT lower(l.wallet), w.slug, w.side, l.block_number, l.log_index,
           l.ts, l.shares, l.usdc/l.shares, l.is_maker, l.taker_fee
    FROM ({_legs_sql(t0, t1)}) l
    INNER JOIN set_windows w ON l.token=w.token
    WHERE lower(l.wallet) IN ({selected}) AND l.bought>0
    ORDER BY wallet, slug, block_number, log_index
    """
    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
    )
    rows = client.query(
        query, settings=SETTINGS, external_data=window_external_data(windows)
    ).result_rows
    fills = [BuyFill(
        str(row[0]), str(row[1]), int(row[2]), (int(row[3]), int(row[4])),
        int(row[5]), float(row[6]), float(row[7]), bool(row[8]), float(row[9]),
    ) for row in rows]
    print(f"period: {iso(args.start)} .. {iso(args.end)} | resolved={len(windows)}")
    print("pairs are opposite-token buys matched FIFO in exact block/log order")
    print("pair sums include explicit taker fees; maker rebates are excluded")
    print(f"{'wallet':<44}{'mkts':>6}{'pairM':>7}{'shares':>11}{'cover':>8}"
          f"{'avgSum':>8}{'<=.98':>8}{'<=.99':>8}{'<=1':>8}{'mk2':>8}"
          f"{'resid':>10}{'d50':>7}{'d90':>7}")
    for wallet in wallets:
        row = summarize_pairs(fills, wallet)
        average = "-" if row.average_sum is None else f"{row.average_sum:.3f}"
        print(f"{wallet:<44}{row.markets:>6}{row.pair_markets:>7}"
              f"{row.paired_shares:>11,.0f}{row.completion_pct:>7.1f}%{average:>8}"
              f"{row.percent(row.under_98_shares):>7.1f}%"
              f"{row.percent(row.under_99_shares):>7.1f}%"
              f"{row.percent(row.under_100_shares):>7.1f}%"
              f"{row.percent(row.both_maker_shares):>7.1f}%"
              f"{row.residual_shares:>10,.0f}{row.median_delay_s:>6.0f}s"
              f"{row.p90_delay_s:>6.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
