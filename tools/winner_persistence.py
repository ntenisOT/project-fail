#!/usr/bin/env python3
"""Does winner ranking persist across periods, and which behaviour predicts it?

This is the test that decides whether "copy the winners" is a real strategy or
survivorship bias. Ranking 300 wallets by realised PnL and then studying the
top guarantees they look profitable; the only honest question is whether
period-A performance predicts period-B performance out of sample.

Run tools/top_setters.py twice with --json-output for two adjacent periods,
then point this at both files.

Measured 2026-08-26 (Aug18-22 vs Aug22-25, 78 wallets with >$1k volume in both):

  PnL rank        rho +0.691  z +6.06   persistent (confounded by size)
  volume rank     rho +0.931  z +8.17   persistent (pure size, expected)
  MARGIN rank     rho +0.517  z +4.54   PERSISTENT -> real, repeatable skill
  $/market rank   rho +0.746  z +6.54   PERSISTENT

Copying period-A's top-20 by margin gave 4.03% margin in period B versus a
2.42% population mean, with 0/20 losers.

Feature correlations against NEXT-period margin were the surprise:

  past margin      +0.517  z +4.54  ***
  both-sided %     -0.464  z -4.07  ***   <- two-sided quoting predicts LOWER margin
  volume           -0.363  z -3.18  ***
  fills/market     -0.361  z -3.16  ***
  maker share %    -0.230  z -2.02  **    <- more maker predicts LOWER margin

Caveat that must travel with these numbers: margin is pnl/volume, which is
mechanically higher for selective low-volume traders. High margin is not the
same as high achievable profit, and every tertile had 0 losers, so the sample
period was broadly favourable. Absolute profit at OUR capital is the decision
variable, not margin.

Usage: python tools/winner_persistence.py out/lb_h1.json out/lb_h2.json
"""
from __future__ import annotations

import argparse
import json
import math


def spearman(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    if n < 3:
        return 0.0, 0.0
    order_x = sorted(range(n), key=lambda i: -xs[i])
    order_y = sorted(range(n), key=lambda i: -ys[i])
    rank_x = {i: k for k, i in enumerate(order_x)}
    rank_y = {i: k for k, i in enumerate(order_y)}
    d2 = sum((rank_x[i] - rank_y[i]) ** 2 for i in range(n))
    rho = 1 - 6 * d2 / (n * (n * n - 1))
    return rho, rho * math.sqrt(n - 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("period_a")
    ap.add_argument("period_b")
    ap.add_argument("--min-volume", type=float, default=1000.0)
    args = ap.parse_args()

    a = {w["wallet"].lower(): w for w in json.load(open(args.period_a))["wallets"]}
    b = {w["wallet"].lower(): w for w in json.load(open(args.period_b))["wallets"]}
    get = lambda w, k: (w.get(k) or 0.0)  # noqa: E731
    common = [x for x in a if x in b
              and get(a[x], "volume_usd") > args.min_volume
              and get(b[x], "volume_usd") > args.min_volume]
    n = len(common)
    if n < 10:
        raise SystemExit(f"only {n} common wallets; need more overlap")
    print(f"common wallets in both periods (>${args.min_volume:,.0f} volume): {n}\n")

    margin = lambda w: get(w, "pnl_usd") / max(get(w, "volume_usd"), 1)  # noqa: E731
    ranks = [
        ("PnL (size-confounded)", lambda x: get(a[x], "pnl_usd"),
         lambda x: get(b[x], "pnl_usd")),
        ("volume (pure size)", lambda x: get(a[x], "volume_usd"),
         lambda x: get(b[x], "volume_usd")),
        ("MARGIN pnl/vol (skill)", lambda x: margin(a[x]), lambda x: margin(b[x])),
        ("$/market", lambda x: get(a[x], "pnl_usd") / max(get(a[x], "market_windows"), 1),
         lambda x: get(b[x], "pnl_usd") / max(get(b[x], "market_windows"), 1)),
    ]
    print(f"{'quantity':<26} {'rho':>7} {'z':>7}  verdict")
    for name, fa, fb in ranks:
        rho, z = spearman([fa(x) for x in common], [fb(x) for x in common])
        print(f"{name:<26} {rho:>+7.3f} {z:>+7.2f}  "
              f"{'PERSISTENT' if abs(z) > 1.96 else 'not persistent'}")

    target = [margin(b[x]) for x in common]
    feats = {
        "past margin": [margin(a[x]) for x in common],
        "both-sided %": [get(a[x], "both_pct") for x in common],
        "volume": [get(a[x], "volume_usd") for x in common],
        "fills/market": [get(a[x], "fills") / max(get(a[x], "market_windows"), 1)
                         for x in common],
        "maker share %": [get(a[x], "maker_share_pct") for x in common],
        "merge sets": [get(a[x], "direct_merge_sets") for x in common],
        "markets traded": [get(a[x], "market_windows") for x in common],
        "taker fees": [get(a[x], "taker_fees_usd") for x in common],
    }
    print(f"\n{'feature -> NEXT-period margin':<30} {'rho':>7} {'z':>7}")
    scored = []
    for name, values in feats.items():
        rho, z = spearman(values, target)
        scored.append((abs(z), name, rho, z))
    for _, name, rho, z in sorted(scored, reverse=True):
        star = "***" if abs(z) > 2.58 else "**" if abs(z) > 1.96 else ""
        print(f"{name:<30} {rho:>+7.3f} {z:>+7.2f}  {star}")

    top = sorted(common, key=lambda x: -margin(a[x]))[:20]
    print(f"\ncopying period-A top-20 by margin into period B:")
    print(f"  mean margin {100*sum(margin(b[x]) for x in top)/len(top):.2f}%  "
          f"(population {100*sum(target)/n:.2f}%)  "
          f"losers {sum(1 for x in top if get(b[x],'pnl_usd')<0)}/{len(top)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
