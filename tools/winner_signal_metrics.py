"""Pure whole-block aggregation and descriptive signal metrics."""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from tools.binance_history import SecondBar
from tools.market_windows import ResolvedWindow
from tools.wallet_pairs import BuyFill


CUTOFF_MARGINS_S = (5, 10, 20)
LOOKBACK_HORIZONS_S = (5, 10, 30)
ROLES = ("maker", "taker")
SOURCES = ("spot", "futures")
MIN_INFERENCE_WALLETS = 20
MIN_INFERENCE_WINDOWS = 200
MIN_INFERENCE_UTC_DAYS = 10


@dataclasses.dataclass
class _ActionAccumulator:
    up_shares: float = 0.0
    down_shares: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    fills: int = 0


@dataclasses.dataclass(frozen=True)
class BlockAction:
    split: str
    wallet: str
    slug: str
    window_start: int
    block_number: int
    block_ts: int
    role: str
    winner_up: bool
    up_shares: float
    down_shares: float
    up_cost: float
    down_cost: float
    fills: int

    @property
    def gross_shares(self) -> float:
        return self.up_shares + self.down_shares

    @property
    def signed_shares(self) -> float:
        return self.up_shares - self.down_shares

    @property
    def direction(self) -> int:
        return _sign(self.signed_shares)

    @property
    def terminal_markout(self) -> float:
        payout = self.up_shares if self.winner_up else self.down_shares
        return payout - self.up_cost - self.down_cost

    @property
    def neutral_pair_markout(self) -> float:
        paired = min(self.up_shares, self.down_shares)
        if paired <= 0:
            return 0.0
        return paired * (
            1 - self.up_cost / self.up_shares - self.down_cost / self.down_shares
        )

    @property
    def directional_markout(self) -> float:
        return self.terminal_markout - self.neutral_pair_markout


def _sign(value: float) -> int:
    return int(value > 0) - int(value < 0)


def aggregate_block_actions(
    fills: Iterable[BuyFill], windows: Mapping[str, ResolvedWindow],
    holdout_start: int,
) -> tuple[list[BlockAction], dict[str, int]]:
    grouped: dict[
        tuple[str, str, str, int, int, str], _ActionAccumulator
    ] = defaultdict(_ActionAccumulator)
    observed = event_fills = outside = unknown = 0
    for fill in fills:
        observed += 1
        window = windows.get(fill.slug)
        if window is None:
            unknown += 1
            continue
        if not window.start <= fill.ts < window.start + 300:
            outside += 1
            continue
        event_fills += 1
        split = "discovery" if window.start < holdout_start else "holdout"
        role = "maker" if fill.is_maker else "taker"
        row = grouped[(
            split, fill.wallet, fill.slug, fill.order[0], fill.ts, role,
        )]
        cost = fill.shares * fill.net_price
        if fill.side == 1:
            row.up_shares += fill.shares
            row.up_cost += cost
        else:
            row.down_shares += fill.shares
            row.down_cost += cost
        row.fills += 1

    actions = [
        BlockAction(
            split, wallet, slug, windows[slug].start, block, block_ts, role,
            bool(windows[slug].winner_up), row.up_shares, row.down_shares,
            row.up_cost, row.down_cost, row.fills,
        )
        for (split, wallet, slug, block, block_ts, role), row in grouped.items()
    ]
    actions.sort(key=lambda row: (
        row.split, row.wallet, row.slug, row.block_number, row.block_ts, row.role,
    ))
    return actions, {
        "fetched_buy_fills": observed,
        "event_window_buy_fills": event_fills,
        "outside_event_window_buy_fills": outside,
        "unknown_window_buy_fills": unknown,
        "block_action_groups": len(actions),
    }


def lagged_return_bp(
    bars: Mapping[int, SecondBar], block_ts: int, margin_s: int, horizon_s: int,
) -> float | None:
    """Return over complete bars ending strictly before the cutoff margin."""
    latest_complete = block_ts - margin_s - 1
    end_bar, start_bar = bars.get(latest_complete), bars.get(
        latest_complete - horizon_s
    )
    if (end_bar is None or start_bar is None
            or end_bar.close <= 0 or start_bar.close <= 0):
        return None
    return 10_000 * math.log(end_bar.close / start_bar.close)


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 9)


def summarize_association(
    actions: Sequence[BlockAction], bars: Mapping[int, SecondBar], *,
    split: str, role: str, source: str, margin_s: int, horizon_s: int,
) -> dict[str, object]:
    selected = [row for row in actions if row.split == split and row.role == role]
    eligible: list[tuple[BlockAction, float]] = []
    missing = 0
    for row in selected:
        feature = lagged_return_bp(bars, row.block_ts, margin_s, horizon_s)
        if feature is None:
            missing += 1
        else:
            eligible.append((row, feature))
    directional = [(row, value) for row, value in eligible if row.direction]
    calls = [(row, value) for row, value in directional if _sign(value)]
    aligned = [(row, value) for row, value in calls
               if row.direction == _sign(value)]
    opposed = [(row, value) for row, value in calls
               if row.direction != _sign(value)]
    directional_shares = sum(abs(row.signed_shares) for row, _ in directional)
    call_shares = sum(abs(row.signed_shares) for row, _ in calls)
    winner_aligned = sum(
        row.direction == (1 if row.winner_up else -1) for row, _ in directional
    )
    feature_winner_aligned = sum(
        _sign(value) == (1 if row.winner_up else -1) for row, value in calls
    )
    wallets, windows = ({row.wallet for row, _ in eligible},
                        {row.slug for row, _ in eligible})
    days = {
        dt.datetime.fromtimestamp(row.window_start, dt.UTC).date().isoformat()
        for row, _ in eligible
    }
    directional_markout = sum(row.directional_markout for row, _ in directional)
    sufficient = (
        len(wallets) >= MIN_INFERENCE_WALLETS
        and len(windows) >= MIN_INFERENCE_WINDOWS
        and len(days) >= MIN_INFERENCE_UTC_DAYS
    )
    return {
        "split": split, "role": role, "source": source,
        "cutoff_margin_s": margin_s, "lookback_horizon_s": horizon_s,
        "action_groups": len(selected), "eligible_feature_groups": len(eligible),
        "missing_feature_groups": missing, "directional_groups": len(directional),
        "nonzero_feature_calls": len(calls), "aligned_groups": len(aligned),
        "opposed_groups": len(opposed),
        "alignment_rate": _rounded(_ratio(len(aligned), len(calls))),
        "winner_alignment_rate": _rounded(_ratio(winner_aligned, len(directional))),
        "feature_winner_alignment_rate": _rounded(
            _ratio(feature_winner_aligned, len(calls))
        ),
        "mean_action_signed_return_bp": _rounded(_ratio(
            sum(row.direction * value for row, value in calls), len(calls)
        )),
        "directional_share_weighted_signed_return_bp": _rounded(_ratio(
            sum(abs(row.signed_shares) * row.direction * value
                for row, value in calls), call_shares,
        )),
        "gross_shares": _rounded(sum(row.gross_shares for row, _ in eligible)),
        "directional_shares": _rounded(directional_shares),
        "terminal_markout_usd": _rounded(sum(
            row.terminal_markout for row, _ in eligible
        )),
        "neutral_pair_markout_usd": _rounded(sum(
            row.neutral_pair_markout for row, _ in eligible
        )),
        "directional_markout_usd": _rounded(directional_markout),
        "directional_markout_per_share": _rounded(_ratio(
            directional_markout, directional_shares
        )),
        "aligned_directional_markout_usd": _rounded(sum(
            row.directional_markout for row, _ in aligned
        )),
        "opposed_directional_markout_usd": _rounded(sum(
            row.directional_markout for row, _ in opposed
        )),
        "wallets": len(wallets), "windows": len(windows), "utc_days": len(days),
        "cluster_counts_sufficient_for_inference": sufficient,
        "claim_level": "exploratory_descriptive_only",
    }
