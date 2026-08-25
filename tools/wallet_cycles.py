#!/usr/bin/env python3
"""Measure ordered complete-set buy-to-sell cycles for selected wallets."""

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
class Fill:
    wallet: str
    slug: str
    side: int
    order: tuple[int, int]
    ts: int
    buying: bool
    shares: float
    price: float
    is_maker: bool = False
    taker_fee: float = 0.0


@dataclasses.dataclass(frozen=True)
class CycleSummary:
    wallet: str
    markets: int
    cycle_markets: int
    shares: float
    buy_value: float
    sell_value: float
    uncovered_sell_shares: float
    median_hold_s: float

    @property
    def buy_sum(self) -> float | None:
        return self.buy_value / self.shares if self.shares else None

    @property
    def sell_sum(self) -> float | None:
        return self.sell_value / self.shares if self.shares else None

    @property
    def edge(self) -> float:
        return self.sell_value - self.buy_value


@dataclasses.dataclass
class _BuyLot:
    order: tuple[int, int]
    ts: int
    shares: float
    price: float


@dataclasses.dataclass
class _RoundTrip:
    buy_order: tuple[int, int]
    buy_ts: int
    sell_order: tuple[int, int]
    sell_ts: int
    shares: float
    buy_price: float
    sell_price: float


@dataclasses.dataclass(frozen=True)
class InventoryCycleSummary:
    wallet: str
    markets: int
    cycle_markets: int
    shares: float
    edge: float
    losing_shares: float
    sell_first_shares: float
    maker_shares: float
    open_shares: float
    median_hold_s: float


@dataclasses.dataclass
class _InventoryLot:
    ts: int
    shares: float
    price: float
    is_maker: bool


@dataclasses.dataclass(frozen=True)
class _InventoryTrip:
    shares: float
    edge_per_share: float
    hold_s: int
    sell_first: bool
    both_maker: bool


def _net_price(fill: Fill) -> float:
    fee_per_share = fill.taker_fee / fill.shares if fill.shares else 0.0
    return fill.price + fee_per_share if fill.buying else fill.price - fee_per_share


def _round_trips(fills: list[Fill], side: int) -> tuple[list[_RoundTrip], float]:
    buys: deque[_BuyLot] = deque()
    trips: list[_RoundTrip] = []
    uncovered_sells = 0.0
    for fill in sorted(fills, key=lambda row: row.order):
        if fill.side != side:
            continue
        if fill.buying:
            buys.append(_BuyLot(fill.order, fill.ts, fill.shares, _net_price(fill)))
            continue
        remaining = fill.shares
        while remaining > 1e-9 and buys:
            lot = buys[0]
            matched = min(remaining, lot.shares)
            trips.append(_RoundTrip(
                lot.order, lot.ts, fill.order, fill.ts,
                matched, lot.price, _net_price(fill),
            ))
            remaining -= matched
            lot.shares -= matched
            if lot.shares <= 1e-9:
                buys.popleft()
        uncovered_sells += remaining
    return trips, uncovered_sells


def _weighted_median(values: list[tuple[float, float]]) -> float:
    midpoint = sum(weight for _, weight in values) / 2
    cumulative = 0.0
    for value, weight in sorted(values):
        cumulative += weight
        if cumulative >= midpoint:
            return value
    return 0.0


def summarize_cycles(fills: list[Fill], wallet: str) -> CycleSummary:
    markets: dict[str, list[Fill]] = defaultdict(list)
    for fill in fills:
        if fill.wallet == wallet:
            markets[fill.slug].append(fill)
    cycle_markets = 0
    shares = buy_value = sell_value = uncovered_sells = 0.0
    holds: list[tuple[float, float]] = []
    for rows in markets.values():
        up, uncovered_up = _round_trips(rows, 1)
        down, uncovered_down = _round_trips(rows, 0)
        up_trips, down_trips = deque(up), deque(down)
        uncovered_sells += uncovered_up + uncovered_down
        market_shares = 0.0
        while up_trips and down_trips:
            up_lot, down_lot = up_trips[0], down_trips[0]
            if up_lot.sell_order < down_lot.buy_order:
                up_trips.popleft()
                continue
            if down_lot.sell_order < up_lot.buy_order:
                down_trips.popleft()
                continue
            matched = min(up_lot.shares, down_lot.shares)
            shares += matched
            market_shares += matched
            buy_value += matched * (up_lot.buy_price + down_lot.buy_price)
            sell_value += matched * (up_lot.sell_price + down_lot.sell_price)
            complete_buy = max(up_lot.buy_ts, down_lot.buy_ts)
            complete_sell = max(up_lot.sell_ts, down_lot.sell_ts)
            holds.append((complete_sell - complete_buy, matched))
            up_lot.shares -= matched
            down_lot.shares -= matched
            if up_lot.shares <= 1e-9:
                up_trips.popleft()
            if down_lot.shares <= 1e-9:
                down_trips.popleft()
        cycle_markets += int(market_shares > 0)
    return CycleSummary(
        wallet, len(markets), cycle_markets, shares, buy_value, sell_value,
        uncovered_sells, _weighted_median(holds) if holds else 0.0,
    )


def _inventory_trips(fills: list[Fill], side: int) -> tuple[list[_InventoryTrip], float]:
    buys: deque[_InventoryLot] = deque()
    sells: deque[_InventoryLot] = deque()
    trips: list[_InventoryTrip] = []
    for fill in sorted(fills, key=lambda row: row.order):
        if fill.side != side:
            continue
        remaining = fill.shares
        fill_price = _net_price(fill)
        opposite = sells if fill.buying else buys
        while remaining > 1e-9 and opposite:
            lot = opposite[0]
            matched = min(remaining, lot.shares)
            sell_price = lot.price if fill.buying else fill_price
            buy_price = fill_price if fill.buying else lot.price
            trips.append(_InventoryTrip(
                matched, sell_price - buy_price, fill.ts - lot.ts,
                fill.buying, lot.is_maker and fill.is_maker,
            ))
            remaining -= matched
            lot.shares -= matched
            if lot.shares <= 1e-9:
                opposite.popleft()
        if remaining > 1e-9:
            target = buys if fill.buying else sells
            target.append(_InventoryLot(fill.ts, remaining, fill_price, fill.is_maker))
    return trips, sum(lot.shares for lot in buys) + sum(lot.shares for lot in sells)


def summarize_inventory_cycles(fills: list[Fill], wallet: str) -> InventoryCycleSummary:
    markets: dict[str, list[Fill]] = defaultdict(list)
    for fill in fills:
        if fill.wallet == wallet:
            markets[fill.slug].append(fill)
    all_trips: list[_InventoryTrip] = []
    open_shares = 0.0
    cycle_markets = 0
    for rows in markets.values():
        market_trips: list[_InventoryTrip] = []
        for side in (0, 1):
            trips, opened = _inventory_trips(rows, side)
            market_trips.extend(trips)
            open_shares += opened
        all_trips.extend(market_trips)
        cycle_markets += int(bool(market_trips))
    shares = sum(trip.shares for trip in all_trips)
    return InventoryCycleSummary(
        wallet, len(markets), cycle_markets, shares,
        sum(trip.shares * trip.edge_per_share for trip in all_trips),
        sum(trip.shares for trip in all_trips if trip.edge_per_share < 0),
        sum(trip.shares for trip in all_trips if trip.sell_first),
        sum(trip.shares for trip in all_trips if trip.both_maker),
        open_shares,
        _weighted_median([(trip.hold_s, trip.shares) for trip in all_trips])
        if all_trips else 0.0,
    )


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_timestamp, required=True)
    parser.add_argument("--end", type=parse_timestamp, required=True)
    parser.add_argument("--wallet", action="append", required=True)
    parser.add_argument("--assets", default="btc,eth,sol,xrp")
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    return parser.parse_args(argv)


def _format_sum(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


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
           l.ts, l.bought>0, l.shares, l.usdc/l.shares, l.is_maker, l.taker_fee
    FROM ({_legs_sql(t0, t1)}) l
    INNER JOIN set_windows w ON l.token=w.token
    WHERE lower(l.wallet) IN ({selected})
    ORDER BY wallet, slug, block_number, log_index
    """
    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
    )
    rows = client.query(
        query, settings=SETTINGS, external_data=window_external_data(windows)
    ).result_rows
    fills = [Fill(str(row[0]), str(row[1]), int(row[2]), (int(row[3]), int(row[4])),
                  int(row[5]), bool(row[6]), float(row[7]), float(row[8]), bool(row[9]),
                  float(row[10]))
             for row in rows]
    print(f"period: {iso(args.start)} .. {iso(args.end)} | resolved={len(windows)}")
    print("cycles require FIFO token round trips with overlapping holding intervals")
    print("cycle prices and edge are net of explicit taker fees; rebates are excluded")
    print("uncovered sells exclude transfers and therefore are not proof of minting")
    print(f"{'wallet':<44}{'mkts':>6}{'cycM':>6}{'shares':>10}{'buySum':>8}"
          f"{'sellSum':>9}{'edge$':>10}{'uncovSell':>10}{'hold':>7}")
    for wallet in wallets:
        cycle = summarize_cycles(fills, wallet)
        print(f"{wallet:<44}{cycle.markets:>6}{cycle.cycle_markets:>6}"
              f"{cycle.shares:>10,.0f}{_format_sum(cycle.buy_sum):>8}"
              f"{_format_sum(cycle.sell_sum):>9}{cycle.edge:>10,.0f}"
              f"{cycle.uncovered_sell_shares:>10,.0f}{cycle.median_hold_s:>6.0f}s")
    print("\nsame-token cycles net alternating buys and sells without double-counting")
    print(f"{'wallet':<44}{'mkts':>6}{'cycM':>6}{'shares':>10}{'edge$':>10}"
          f"{'c/sh':>8}{'loss%':>8}{'sell1%':>8}{'mk2%':>8}{'open':>10}{'hold':>7}")
    for wallet in wallets:
        inventory = summarize_inventory_cycles(fills, wallet)
        per_share = 100 * inventory.edge / inventory.shares if inventory.shares else 0.0
        loss_pct = (100 * inventory.losing_shares / inventory.shares
                    if inventory.shares else 0.0)
        sell_pct = (100 * inventory.sell_first_shares / inventory.shares
                    if inventory.shares else 0.0)
        maker_pct = (100 * inventory.maker_shares / inventory.shares
                     if inventory.shares else 0.0)
        print(f"{wallet:<44}{inventory.markets:>6}{inventory.cycle_markets:>6}"
              f"{inventory.shares:>10,.0f}{inventory.edge:>10,.0f}{per_share:>8.2f}"
              f"{loss_pct:>7.1f}%{sell_pct:>7.1f}%{maker_pct:>7.1f}%"
              f"{inventory.open_shares:>10,.0f}{inventory.median_hold_s:>6.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
