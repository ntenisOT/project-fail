#!/usr/bin/env python3
"""Compare wallet trade-flow direction with the contemporaneous public tape."""

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

from tools.clickhouse_forensics import SETTINGS, window_external_data
from tools.market_windows import ASSET_PREFIX, ResolvedWindow, resolve_windows
from tools.top_setters import DEFAULT_CACHE, iso, parse_timestamp
from tools.wallet_signal import Fill, WALLET_RE, fetch_fills


@dataclasses.dataclass(frozen=True)
class TapeMark:
    slug: str
    cutoff_s: int
    up_price: float


@dataclasses.dataclass(frozen=True)
class AlignmentSummary:
    calls: int
    wallet_hits: int
    tape_hits: int
    agreements: int
    disagreements: int
    wallet_disagreement_wins: int


@dataclasses.dataclass(frozen=True)
class TapeEdgeSummary:
    calls: int
    wins: int
    average_price: float
    ev_per_share: float


def summarize_alignment(
    fills: Sequence[Fill], marks: Sequence[TapeMark], wallet: str,
    cutoff_s: int, min_call_shares: float = 5.0,
) -> AlignmentSummary:
    mark_by_slug = {mark.slug: mark for mark in marks if mark.cutoff_s == cutoff_s}
    markets: dict[str, list[Fill]] = defaultdict(list)
    for fill in fills:
        if fill.wallet == wallet and fill.ts < fill.start + cutoff_s:
            markets[fill.slug].append(fill)
    calls = wallet_hits = tape_hits = agreements = disagreements = wallet_wins = 0
    for slug, rows in markets.items():
        mark = mark_by_slug.get(slug)
        if mark is None or abs(mark.up_price - 0.5) < 1e-9:
            continue
        winner_up = rows[0].winner_up
        if any(row.winner_up != winner_up for row in rows):
            raise ValueError(f"inconsistent window metadata for {slug}")
        direction = (sum(row.net_shares for row in rows if row.side_up)
                     - sum(row.net_shares for row in rows if not row.side_up))
        if abs(direction) < min_call_shares:
            continue
        wallet_up = direction > 0
        tape_up = mark.up_price > 0.5
        calls += 1
        wallet_hits += int(wallet_up == winner_up)
        tape_hits += int(tape_up == winner_up)
        if wallet_up == tape_up:
            agreements += 1
        else:
            disagreements += 1
            wallet_wins += int(wallet_up == winner_up)
    return AlignmentSummary(
        calls, wallet_hits, tape_hits, agreements, disagreements, wallet_wins,
    )


def summarize_tape_edge(
    marks: Sequence[TapeMark], windows: Sequence[ResolvedWindow], cutoff_s: int,
    slippage: float,
) -> TapeEdgeSummary:
    winner = {window.slug: bool(window.winner_up) for window in windows}
    calls = wins = 0
    price_total = pnl_total = 0.0
    for mark in marks:
        if mark.cutoff_s != cutoff_s or abs(mark.up_price - 0.5) < 1e-9:
            continue
        favorite_up = mark.up_price > 0.5
        price = mark.up_price if favorite_up else 1 - mark.up_price
        execution = min(price + slippage, 0.999)
        fee = 0.07 * execution * (1 - execution)
        won = favorite_up == winner[mark.slug]
        calls += 1
        wins += int(won)
        price_total += price
        pnl_total += int(won) - execution - fee
    return TapeEdgeSummary(
        calls, wins, price_total / calls if calls else 0.0,
        pnl_total / calls if calls else 0.0,
    )


def fetch_marks(client, windows, cutoffs: Sequence[int]) -> list[TapeMark]:
    t0 = min(window.start for window in windows)
    t1 = max(window.start for window in windows) + 300
    values = ",".join(str(cutoff) for cutoff in cutoffs if cutoff > 0)
    query = f"""
    SELECT w.slug, cutoff,
           argMax(t.price, tuple(t.ts, t.block_number, t.log_index))
    FROM (
      SELECT multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) token,
             toUInt32(block_timestamp) ts, block_number, log_index,
             toFloat64(if(maker_asset_id='0', maker_amount_filled,
                          taker_amount_filled))/1e6 usdc,
             toFloat64(if(maker_asset_id='0', taker_amount_filled,
                          maker_amount_filled))/1e6 shares,
             usdc/shares price
      FROM trade_history
      WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
        AND (maker_asset_id IN (SELECT token FROM set_windows)
             OR taker_asset_id IN (SELECT token FROM set_windows))
    ) t
    INNER JOIN (SELECT slug, start_ts, token FROM set_windows WHERE side=1) w
      ON t.token=w.token
    ARRAY JOIN [{values}] AS cutoff
    WHERE t.ts>=w.start_ts AND t.ts<w.start_ts+cutoff AND t.shares>0
    GROUP BY w.slug, cutoff
    """
    rows = client.query(
        query, settings=SETTINGS, external_data=window_external_data(windows)
    ).result_rows
    return [TapeMark(str(row[0]), int(row[1]), float(row[2])) for row in rows]


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_timestamp, required=True)
    parser.add_argument("--end", type=parse_timestamp, required=True)
    parser.add_argument("--wallet", action="append", required=True)
    parser.add_argument("--assets", default="btc,eth,sol,xrp")
    parser.add_argument("--cutoffs", default="30,60,90,120,150,180,210,240,270,300")
    parser.add_argument("--min-call-shares", type=float, default=5.0)
    parser.add_argument("--slippage", type=float, default=0.01)
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    wallets = sorted({wallet.lower() for wallet in args.wallet})
    assets = [value.strip().lower() for value in args.assets.split(",") if value.strip()]
    cutoffs = sorted({int(value) for value in args.cutoffs.split(",")})
    if (any(not WALLET_RE.fullmatch(wallet) for wallet in wallets)
            or not assets or set(assets) - set(ASSET_PREFIX) or args.end < args.start
            or not cutoffs or min(cutoffs) <= 0 or max(cutoffs) > 300
            or args.min_call_shares < 0 or not 0 <= args.slippage < 0.1):
        raise SystemExit("invalid wallet, assets, period, cutoffs, or call threshold")
    windows, _ = resolve_windows(
        assets, args.start, args.end, args.cache,
        fetch_missing=False, allow_missing=False,
    )
    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
    )
    fills = fetch_fills(client, windows, wallets)
    marks = fetch_marks(client, windows, cutoffs)
    print(f"period: {iso(args.start)} .. {iso(args.end)} | resolved={len(windows)}")
    print("wallet call is net trade-flow inventory; tape call is last Up trade above/below 50c")
    print(f"{'wallet':<44}{'t':>5}{'calls':>7}{'wallet%':>9}{'tape%':>8}"
          f"{'agree%':>8}{'dis':>6}{'wallet wins':>13}")
    for wallet in wallets:
        for cutoff in cutoffs:
            alignment = summarize_alignment(
                fills, marks, wallet, cutoff, args.min_call_shares,
            )
            wallet_pct = (100 * alignment.wallet_hits / alignment.calls
                          if alignment.calls else 0.0)
            tape_pct = (100 * alignment.tape_hits / alignment.calls
                        if alignment.calls else 0.0)
            agree_pct = (100 * alignment.agreements / alignment.calls
                         if alignment.calls else 0.0)
            dis_wins = (100 * alignment.wallet_disagreement_wins
                        / alignment.disagreements if alignment.disagreements else 0.0)
            print(f"{wallet:<44}{cutoff:>4}s{alignment.calls:>7}{wallet_pct:>8.1f}%"
                  f"{tape_pct:>7.1f}%{agree_pct:>7.1f}%{alignment.disagreements:>6}"
                  f"{dis_wins:>11.1f}%")
    print(f"\npublic-tape favorite proxy after {args.slippage:.0%} slippage plus taker fee")
    print("last trade is not an executable quote; this is an optimistic screen")
    print(f"{'t':>5}{'calls':>7}{'win%':>8}{'avg price':>11}{'EV/share':>11}")
    for cutoff in cutoffs:
        edge = summarize_tape_edge(marks, windows, cutoff, args.slippage)
        win_pct = 100 * edge.wins / edge.calls if edge.calls else 0.0
        print(f"{cutoff:>4}s{edge.calls:>7}{win_pct:>7.1f}%"
              f"{edge.average_price:>11.3f}{100*edge.ev_per_share:>+10.2f}c")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
