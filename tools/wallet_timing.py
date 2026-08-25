#!/usr/bin/env python3
"""Measure when selected wallets trade relative to each five-minute event."""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import Sequence

import clickhouse_connect  # type: ignore[import-untyped]

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.clickhouse_forensics import SETTINGS, _legs_sql, window_external_data
from tools.market_windows import ASSET_PREFIX, resolve_windows
from tools.top_setters import DEFAULT_CACHE, iso, parse_timestamp


WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def percent(value: float, total: float) -> float:
    return 100 * float(value) / float(total) if total else 0.0


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
    if any(not WALLET_RE.fullmatch(wallet) for wallet in wallets):
        raise SystemExit("every --wallet must be a 0x-prefixed 20-byte address")
    assets = [value.strip().lower() for value in args.assets.split(",") if value.strip()]
    if not assets or set(assets) - set(ASSET_PREFIX) or args.end < args.start:
        raise SystemExit("invalid assets or period")
    windows, _ = resolve_windows(
        assets, args.start, args.end, args.cache,
        fetch_missing=False, allow_missing=False,
    )
    t0 = min(window.start for window in windows) - 26 * 60 * 60
    t1 = max(window.start for window in windows) + 60 * 60
    selected = ",".join(f"'{wallet}'" for wallet in wallets)
    query = f"""
    SELECT wallet, count(), sum(volume), sum(pre_volume), sum(event_volume),
           sum(early_volume), sum(middle_volume), sum(late_volume),
           sum(post_volume), sum(maker_volume), sum(fills),
           countIf(event_fills>0),
           countIf(event_fills>0 AND first_event<=60 AND last_event>=240),
           sum(edge), sum(pre_edge), sum(early_edge), sum(middle_edge),
           sum(late_edge), sum(post_edge)
    FROM (
      SELECT lower(l.wallet) wallet, w.slug,
             sum(l.usdc) volume,
             sumIf(l.usdc, l.ts<w.start_ts) pre_volume,
             sumIf(l.usdc, l.ts>=w.start_ts AND l.ts<w.start_ts+300) event_volume,
             sumIf(l.usdc, l.ts>=w.start_ts AND l.ts<w.start_ts+60) early_volume,
             sumIf(l.usdc, l.ts>=w.start_ts+60 AND l.ts<w.start_ts+240) middle_volume,
             sumIf(l.usdc, l.ts>=w.start_ts+240 AND l.ts<w.start_ts+300) late_volume,
             sumIf(l.usdc, l.ts>=w.start_ts+300) post_volume,
             sumIf(l.usdc, l.is_maker) maker_volume,
             sum(l.cash-l.taker_fee+l.net_shares*w.payoff) edge,
             sumIf(l.cash-l.taker_fee+l.net_shares*w.payoff, l.ts<w.start_ts) pre_edge,
             sumIf(l.cash-l.taker_fee+l.net_shares*w.payoff,
                   l.ts>=w.start_ts AND l.ts<w.start_ts+60) early_edge,
             sumIf(l.cash-l.taker_fee+l.net_shares*w.payoff,
                   l.ts>=w.start_ts+60 AND l.ts<w.start_ts+240) middle_edge,
             sumIf(l.cash-l.taker_fee+l.net_shares*w.payoff,
                   l.ts>=w.start_ts+240 AND l.ts<w.start_ts+300) late_edge,
             sumIf(l.cash-l.taker_fee+l.net_shares*w.payoff,
                   l.ts>=w.start_ts+300) post_edge,
             count() fills,
             countIf(l.ts>=w.start_ts AND l.ts<w.start_ts+300) event_fills,
             minIf(toInt64(l.ts)-toInt64(w.start_ts),
                   l.ts>=w.start_ts AND l.ts<w.start_ts+300) first_event,
             maxIf(toInt64(l.ts)-toInt64(w.start_ts),
                   l.ts>=w.start_ts AND l.ts<w.start_ts+300) last_event
      FROM ({_legs_sql(t0, t1)}) l
      INNER JOIN set_windows w ON l.token=w.token
      WHERE lower(l.wallet) IN ({selected})
      GROUP BY lower(l.wallet), w.slug
    )
    GROUP BY wallet
    ORDER BY wallet
    """
    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
    )
    rows = client.query(
        query, settings=SETTINGS, external_data=window_external_data(windows)
    ).result_rows
    print(f"period: {iso(args.start)} .. {iso(args.end)} | resolved={len(windows)}")
    print(f"{'wallet':<44}{'mkts':>6}{'vol$':>11}{'pre%':>7}{'early%':>8}"
          f"{'mid%':>7}{'late%':>7}{'post%':>7}{'maker%':>8}"
          f"{'eventM%':>9}{'span%':>7}{'fills':>8}")
    for (wallet, markets, volume, pre, _event, early, middle, late, post, maker,
         fills, event_markets, spans, _edge, _pre_edge, _early_edge,
         _middle_edge, _late_edge, _post_edge) in rows:
        print(f"{wallet:<44}{markets:>6}{volume:>11,.0f}{percent(pre, volume):>6.1f}%"
              f"{percent(early, volume):>7.1f}%{percent(middle, volume):>6.1f}%"
              f"{percent(late, volume):>6.1f}%{percent(post, volume):>6.1f}%"
              f"{percent(maker, volume):>7.1f}%{percent(event_markets, markets):>8.1f}%"
              f"{percent(spans, markets):>6.1f}%{fills:>8}")
    print("\nterminal markout by fill time (cash plus shares times official payoff)")
    print(f"{'wallet':<44}{'edge$':>10}{'pre$':>10}{'0-60$':>10}"
          f"{'60-240$':>11}{'240-300$':>12}{'post$':>10}")
    for row in rows:
        wallet = row[0]
        edge, pre, early, middle, late, post = (float(value) for value in row[-6:])
        print(f"{wallet:<44}{edge:>10,.0f}{pre:>10,.0f}{early:>10,.0f}"
              f"{middle:>11,.0f}{late:>12,.0f}{post:>10,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
