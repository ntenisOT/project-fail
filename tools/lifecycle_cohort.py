#!/usr/bin/env python3
"""Time-ordered FIFO lifecycle study selected by volume, never by PnL."""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import pathlib
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

import clickhouse_connect  # type: ignore[import-untyped]

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.clickhouse_forensics import fetch_direct_ctf, fetch_token_activity
from tools.market_windows import ASSET_PREFIX, ResolvedWindow, resolve_windows
from tools.top_setters import DEFAULT_CACHE, iso, parse_timestamp
from tools.wallet_metrics import TokenActivity, WalletSummary, summarize_wallets
from tools.wallet_pairs import BuyFill, PairSummary, fetch_buy_fills, summarize_pairs


@dataclasses.dataclass(frozen=True)
class LifecycleSummary:
    wallet: str
    markets: int
    volume: float
    actual_pnl: float
    neutral_pnl: float
    directional_pnl: float
    maker_pct: float
    taker_fees: float
    paired_shares: float
    pair_completion_pct: float
    fifo_pair_sum: float | None
    both_maker_pct: float
    under_99_pct: float
    pair_delay_p50_s: float
    pair_delay_p90_s: float
    residual_markets: int
    residual_hit_pct: float
    residual_weighted_hit_pct: float
    direct_split_sets: float
    direct_merge_sets: float
    classification: str
    selection_sources: tuple[str, ...] = ()

    @property
    def neutral_rov(self) -> float:
        return self.neutral_pnl / self.volume if self.volume else 0.0

    @property
    def actual_rov(self) -> float:
        return self.actual_pnl / self.volume if self.volume else 0.0

    def as_dict(self) -> dict[str, object]:
        result = dataclasses.asdict(self)
        result["neutral_rov"] = self.neutral_rov
        result["actual_rov"] = self.actual_rov
        return result


def _classification(base: WalletSummary, pairs: PairSummary, neutral: float) -> str:
    if pairs.paired_shares and base.direct_merge_sets >= 0.25 * pairs.paired_shares:
        return "merge_recycler"
    directional = base.pnl - neutral
    if abs(directional) > abs(neutral) and abs(directional) >= 1:
        return "direction_dominated"
    if pairs.completion_pct >= 80 and neutral > 0:
        return "neutral_pair_accumulator"
    return "mixed_or_unprofitable"


def summarize_lifecycles(
    activity: Iterable[TokenActivity], fills: list[BuyFill],
    windows: Sequence[ResolvedWindow],
    direct_ctf: Mapping[tuple[str, str], tuple[float, float]],
    wallets: Sequence[str],
    selection_sources: Mapping[str, tuple[str, ...]] | None = None,
) -> list[LifecycleSummary]:
    selection_sources = selection_sources or {}
    activity_rows = list(activity)
    bases = {row.wallet: row for row in summarize_wallets(activity_rows, direct_ctf)}
    outcomes = {window.slug: bool(window.winner_up) for window in windows}
    by_market: dict[tuple[str, str], dict[int, TokenActivity]] = defaultdict(dict)
    for row in activity_rows:
        by_market[(row.wallet.lower(), row.slug)][row.side] = row

    result: list[LifecycleSummary] = []
    for wallet in wallets:
        base = bases.get(wallet, WalletSummary(wallet))
        pairs = summarize_pairs(fills, wallet)
        neutral = 0.0
        residual_markets = residual_hits = 0
        residual_weight = aligned_weight = 0.0
        for (candidate, slug), sides in by_market.items():
            if candidate != wallet or slug not in outcomes:
                continue
            for side, row in sides.items():
                payoff = float(outcomes[slug] if side == 1 else not outcomes[slug])
                neutral += row.pnl - row.net_shares * payoff + 0.5 * row.net_shares
            up_row, down_row = sides.get(1), sides.get(0)
            imbalance = (up_row.net_shares if up_row is not None else 0.0) - (
                down_row.net_shares if down_row is not None else 0.0
            )
            if abs(imbalance) <= 0.1:
                continue
            residual_markets += 1
            aligned = imbalance > 0 if outcomes[slug] else imbalance < 0
            residual_hits += int(aligned)
            residual_weight += abs(imbalance)
            aligned_weight += abs(imbalance) * int(aligned)
        result.append(LifecycleSummary(
            wallet=wallet,
            markets=base.market_windows,
            volume=base.volume,
            actual_pnl=base.pnl,
            neutral_pnl=neutral,
            directional_pnl=base.pnl - neutral,
            maker_pct=base.maker_share_pct,
            taker_fees=base.taker_fees,
            paired_shares=pairs.paired_shares,
            pair_completion_pct=pairs.completion_pct,
            fifo_pair_sum=pairs.average_sum,
            both_maker_pct=pairs.percent(pairs.both_maker_shares),
            under_99_pct=pairs.percent(pairs.under_99_shares),
            pair_delay_p50_s=pairs.median_delay_s,
            pair_delay_p90_s=pairs.p90_delay_s,
            residual_markets=residual_markets,
            residual_hit_pct=(100 * residual_hits / residual_markets
                              if residual_markets else 0.0),
            residual_weighted_hit_pct=(100 * aligned_weight / residual_weight
                                       if residual_weight else 0.0),
            direct_split_sets=base.direct_split_sets,
            direct_merge_sets=base.direct_merge_sets,
            classification=_classification(base, pairs, neutral),
            selection_sources=selection_sources.get(wallet, ()),
        ))
    return result


def auc(scores: Sequence[float], labels: Sequence[bool]) -> float | None:
    positive = [score for score, label in zip(scores, labels, strict=True) if label]
    negative = [score for score, label in zip(scores, labels, strict=True) if not label]
    if not positive or not negative:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0
               for p in positive for n in negative)
    return wins / (len(positive) * len(negative))


def spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Return tie-aware rank correlation without an optional statistics stack."""
    if len(left) != len(right) or len(left) < 2:
        return None

    def ranks(values: Sequence[float]) -> list[float]:
        result = [0.0] * len(values)
        ordered = sorted(range(len(values)), key=values.__getitem__)
        position = 0
        while position < len(ordered):
            end = position + 1
            while end < len(ordered) and values[ordered[end]] == values[ordered[position]]:
                end += 1
            rank = (position + 1 + end) / 2
            for index in ordered[position:end]:
                result[index] = rank
            position = end
        return result

    left_ranks, right_ranks = ranks(left), ranks(right)
    left_mean = sum(left_ranks) / len(left_ranks)
    right_mean = sum(right_ranks) / len(right_ranks)
    numerator = sum(
        (a - left_mean) * (b - right_mean)
        for a, b in zip(left_ranks, right_ranks, strict=True)
    )
    left_ss = sum((value - left_mean) ** 2 for value in left_ranks)
    right_ss = sum((value - right_mean) ** 2 for value in right_ranks)
    denominator = math.sqrt(left_ss * right_ss)
    return numerator / denominator if denominator else None


def _split_windows(
    windows: Sequence[ResolvedWindow], holdout_fraction: float,
) -> tuple[list[ResolvedWindow], list[ResolvedWindow]]:
    starts = sorted({window.start for window in windows})
    cut = max(1, min(len(starts) - 1, int(len(starts) * (1 - holdout_fraction))))
    holdout_start = starts[cut]
    return (
        [window for window in windows if window.start < holdout_start],
        [window for window in windows if window.start >= holdout_start],
    )


def _print_period(name: str, rows: Sequence[LifecycleSummary]) -> None:
    print(f"\n{name}: volume-selected wallets; PnL did not select the cohort")
    print(f"{'wallet':<44}{'mkts':>6}{'vol$':>10}{'actual$':>10}{'neutral$':>10}"
          f"{'dir$':>9}{'FIFO':>7}{'cover':>7}{'mk2':>7}{'resHit':>8}"
          f"{'merge':>9}  class")
    for row in rows:
        pair_sum = "-" if row.fifo_pair_sum is None else f"{row.fifo_pair_sum:.3f}"
        print(f"{row.wallet:<44}{row.markets:>6}{row.volume:>10,.0f}"
              f"{row.actual_pnl:>+10,.0f}{row.neutral_pnl:>+10,.0f}"
              f"{row.directional_pnl:>+9,.0f}{pair_sum:>7}"
              f"{row.pair_completion_pct:>6.1f}%{row.both_maker_pct:>6.1f}%"
              f"{row.residual_weighted_hit_pct:>7.1f}%"
              f"{row.direct_merge_sets:>9,.0f}  {row.classification}")


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=168)
    parser.add_argument("--start", type=parse_timestamp)
    parser.add_argument("--end", type=parse_timestamp)
    parser.add_argument("--assets", default="btc")
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--holdout-fraction", type=float, default=0.25)
    parser.add_argument("--min-discovery-windows", type=int, default=50)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--merge-additions", type=int, default=10)
    parser.add_argument("--both-additions", type=int, default=10)
    parser.add_argument("--min-holdout-windows", type=int, default=10)
    parser.add_argument(
        "--cohort-from",
        help="Reuse the exact wallet list from a prior lifecycle JSON instead of reselecting",
    )
    parser.add_argument("--json-output")
    return parser.parse_args(argv)


def load_frozen_cohort(
    path: str | pathlib.Path,
) -> tuple[list[str], dict[str, set[str]]]:
    """Load an immutable wallet cohort and its original selection provenance."""
    payload = json.loads(pathlib.Path(path).read_text())
    raw_wallets = payload.get("wallets")
    raw_sources = payload.get("selection_sources", {})
    if not isinstance(raw_wallets, list) or len(raw_wallets) < 2:
        raise ValueError("frozen cohort must contain at least two wallets")
    wallets = [str(wallet).lower() for wallet in raw_wallets]
    if len(set(wallets)) != len(wallets):
        raise ValueError("frozen cohort contains duplicate wallets")
    if not isinstance(raw_sources, dict):
        raise ValueError("frozen cohort selection_sources must be an object")
    sources: dict[str, set[str]] = {}
    for wallet in wallets:
        values = raw_sources.get(wallet, ["frozen"])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"invalid selection sources for {wallet}")
        sources[wallet] = set(values) or {"frozen"}
    return wallets, sources


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    assets = [value.strip().lower() for value in args.assets.split(",") if value.strip()]
    if (not assets or set(assets) - set(ASSET_PREFIX)
            or (args.start is None) != (args.end is None)
            or not 0 < args.holdout_fraction < 0.5 or args.limit <= 1
            or min(args.merge_additions, args.both_additions,
                   args.min_holdout_windows) < 0):
        raise SystemExit("invalid assets, period, holdout, or cohort limit")
    if args.start is None:
        end = (int(time.time()) // 300) * 300 - 900
        periods = max(2, math.ceil(args.hours * 12))
        start = end - (periods - 1) * 300
    else:
        start, end = args.start, args.end
    if end < start:
        raise SystemExit("--end must not be before --start")
    windows, missing = resolve_windows(
        assets, start, end, args.cache, max(1, args.workers),
        fetch_missing=True, allow_missing=False,
    )
    discovery_windows, holdout_windows = _split_windows(windows, args.holdout_fraction)
    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
    )
    discovery_activity = fetch_token_activity(client, discovery_windows)
    discovery_ctf = fetch_direct_ctf(client, discovery_windows)
    if args.cohort_from:
        wallets, selection_sources = load_frozen_cohort(args.cohort_from)
        selection_description = (
            f"exact frozen wallet cohort from {args.cohort_from}; never reselected"
        )
    else:
        discovery_base = summarize_wallets(discovery_activity, discovery_ctf)
        eligible = [row for row in discovery_base
                    if row.market_windows >= args.min_discovery_windows]
        volume_rows = sorted(
            eligible,
            key=lambda row: row.volume, reverse=True,
        )[:args.limit]
        merge_rows = sorted(
            eligible, key=lambda row: (row.direct_merge_sets, row.volume), reverse=True,
        )[:args.merge_additions]
        both_rows = sorted(
            eligible,
            key=lambda row: (row.bought_both_pct, row.maker_share_pct, row.volume),
            reverse=True,
        )[:args.both_additions]
        selection_sources = defaultdict(set)
        for source, rows in (
            ("volume", volume_rows), ("merge", merge_rows), ("both_buy", both_rows),
        ):
            for row in rows:
                selection_sources[row.wallet].add(source)
        wallets = list(selection_sources)
        selection_description = (
            "discovery volume plus merge and both-buy activity; never PnL"
        )
    if len(wallets) < 2:
        raise SystemExit("fewer than two wallets met the volume cohort gate")
    holdout_activity = fetch_token_activity(client, holdout_windows)
    holdout_ctf = fetch_direct_ctf(client, holdout_windows)
    discovery_fills = fetch_buy_fills(client, discovery_windows, wallets)
    holdout_fills = fetch_buy_fills(client, holdout_windows, wallets)
    discovery = summarize_lifecycles(
        discovery_activity, discovery_fills, discovery_windows, discovery_ctf, wallets,
        {wallet: tuple(sorted(sources)) for wallet, sources in selection_sources.items()},
    )
    holdout = summarize_lifecycles(
        holdout_activity, holdout_fills, holdout_windows, holdout_ctf, wallets,
        {wallet: tuple(sorted(sources)) for wallet, sources in selection_sources.items()},
    )
    holdout_by_wallet = {row.wallet: row for row in holdout}
    paired = [
        (row, holdout_by_wallet[row.wallet]) for row in discovery
        if (row.fifo_pair_sum is not None and row.wallet in holdout_by_wallet
            and holdout_by_wallet[row.wallet].markets >= args.min_holdout_windows)
    ]
    pair_scores = [1 - row.fifo_pair_sum for row, _ in paired
                   if row.fifo_pair_sum is not None]
    neutral_labels = [future.neutral_pnl > 0 for row, future in paired
                      if row.fifo_pair_sum is not None]
    actual_labels = [future.actual_pnl > 0 for row, future in paired
                     if row.fifo_pair_sum is not None]
    neutral_auc = auc(pair_scores, neutral_labels)
    actual_auc = auc(pair_scores, actual_labels)
    all_discovery_fifo = [
        (row, holdout_by_wallet[row.wallet]) for row in discovery
        if row.fifo_pair_sum is not None and row.wallet in holdout_by_wallet
    ]
    all_actual_auc = auc(
        [1 - row.fifo_pair_sum for row, _ in all_discovery_fifo
         if row.fifo_pair_sum is not None],
        [future.actual_pnl > 0 for row, future in all_discovery_fifo
         if row.fifo_pair_sum is not None],
    )
    style_pairs = [
        (row.fifo_pair_sum, future.fifo_pair_sum)
        for row, future in all_discovery_fifo
        if row.fifo_pair_sum is not None and future.fifo_pair_sum is not None
    ]
    style_correlation = spearman(
        [row[0] for row in style_pairs], [row[1] for row in style_pairs],
    )
    print(f"period: {iso(start)} .. {iso(end)} | windows={len(windows)} "
          f"missing={len(missing)} | selected={len(wallets)} by discovery activity")
    print(f"discovery={iso(min(w.start for w in discovery_windows))}.."
          f"{iso(max(w.start for w in discovery_windows))} | holdout="
          f"{iso(min(w.start for w in holdout_windows))}.."
          f"{iso(max(w.start for w in holdout_windows))}")
    print("FIFO sums use exact block/log order and explicit taker fees; rebates excluded")
    neutral_text = "unavailable" if neutral_auc is None else f"{neutral_auc:.3f}"
    actual_text = "unavailable" if actual_auc is None else f"{actual_auc:.3f}"
    style_text = ("unavailable" if style_correlation is None else
                  f"{style_correlation:.3f}")
    print(f"cheap-pair holdout-neutral AUC: {neutral_text} | n={len(paired)} | "
          f"minHoldoutWindows={args.min_holdout_windows}"
          + (" | INSUFFICIENT (<20)" if len(paired) < 20 else ""))
    print(f"cheap-pair holdout-actual AUC: {actual_text} | same n={len(paired)}")
    print(f"discovery/holdout FIFO rank persistence: {style_text} | "
          f"n={len(style_pairs)}")
    _print_period("DISCOVERY", discovery)
    _print_period("UNTOUCHED HOLDOUT", holdout)
    if args.json_output:
        payload = {
            "period": {"start": start, "end": end},
            "discovery_end": max(window.start for window in discovery_windows),
            "holdout_start": min(window.start for window in holdout_windows),
            "selection": selection_description,
            "selection_sources": {
                wallet: sorted(sources) for wallet, sources in selection_sources.items()
            },
            "wallets": wallets,
            "cheap_pair_holdout_neutral_auc": neutral_auc,
            "cheap_pair_holdout_actual_auc": actual_auc,
            "cheap_pair_holdout_actual_auc_all_discovery_fifo": all_actual_auc,
            "cheap_pair_auc_n": len(paired),
            "discovery_holdout_fifo_spearman": style_correlation,
            "fifo_persistence_n": len(style_pairs),
            "discovery_fifo_n": len(all_discovery_fifo),
            "holdout_inactive_n": sum(
                future.markets == 0 for _, future in all_discovery_fifo
            ),
            "holdout_no_fifo_n": sum(
                future.fifo_pair_sum is None for _, future in all_discovery_fifo
            ),
            "min_holdout_windows": args.min_holdout_windows,
            "discovery": [row.as_dict() for row in discovery],
            "holdout": [row.as_dict() for row in holdout],
        }
        target = pathlib.Path(args.json_output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
