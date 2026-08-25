"""Market-scoped wallet activity models and aggregation."""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from typing import Iterable, Mapping


@dataclasses.dataclass(frozen=True)
class TokenActivity:
    wallet: str
    slug: str
    asset: str
    start: int
    side: int
    pnl: float
    volume: float
    bought: float
    buy_usdc: float
    sold: float
    sell_usdc: float
    net_shares: float
    maker_volume: float
    fills: int
    maker_fills: int
    buy_fee: float = 0.0
    sell_fee: float = 0.0


@dataclasses.dataclass
class WalletSummary:
    wallet: str
    pnl: float = 0.0
    volume: float = 0.0
    market_windows: int = 0
    both_windows: int = 0
    bought_both_windows: int = 0
    sold_both_windows: int = 0
    balanced_end_windows: int = 0
    sold: float = 0.0
    inventory_floor_shares: float = 0.0
    maker_volume: float = 0.0
    taker_fees: float = 0.0
    fills: int = 0
    net_imbalance_sum: float = 0.0
    direct_split_sets: float = 0.0
    direct_merge_sets: float = 0.0
    paired_buy_cost: float = 0.0
    paired_buy_shares: float = 0.0
    paired_sell_proceeds: float = 0.0
    paired_sell_shares: float = 0.0

    def _pct(self, numerator: float, denominator: float | None = None) -> float:
        base = self.market_windows if denominator is None else denominator
        return 100.0 * numerator / base if base else 0.0

    @property
    def both_pct(self) -> float:
        return self._pct(self.both_windows)

    @property
    def bought_both_pct(self) -> float:
        return self._pct(self.bought_both_windows)

    @property
    def sold_both_pct(self) -> float:
        return self._pct(self.sold_both_windows)

    @property
    def inventory_floor_pct(self) -> float:
        return self._pct(self.inventory_floor_shares, self.sold)

    @property
    def maker_share_pct(self) -> float:
        return self._pct(self.maker_volume, self.volume)

    @property
    def avg_net_imbalance(self) -> float:
        return self.net_imbalance_sum / self.market_windows if self.market_windows else 0.0

    @property
    def buy_pair_sum(self) -> float | None:
        return (self.paired_buy_cost / self.paired_buy_shares
                if self.paired_buy_shares else None)

    @property
    def sell_pair_sum(self) -> float | None:
        return (self.paired_sell_proceeds / self.paired_sell_shares
                if self.paired_sell_shares else None)

    def as_dict(self) -> dict[str, object]:
        return {
            "wallet": self.wallet,
            "pnl_usd": self.pnl,
            "volume_usd": self.volume,
            "market_windows": self.market_windows,
            "both_pct": self.both_pct,
            "bought_both_pct": self.bought_both_pct,
            "sold_both_pct": self.sold_both_pct,
            "inventory_floor_pct": self.inventory_floor_pct,
            "maker_share_pct": self.maker_share_pct,
            "taker_fees_usd": self.taker_fees,
            "avg_net_imbalance_shares": self.avg_net_imbalance,
            "direct_split_sets": self.direct_split_sets,
            "direct_merge_sets": self.direct_merge_sets,
            "buy_pair_sum": self.buy_pair_sum,
            "sell_pair_sum": self.sell_pair_sum,
            "fills": self.fills,
        }


def summarize_wallets(
    activity: Iterable[TokenActivity],
    direct_ctf: Mapping[tuple[str, str], tuple[float, float]] | None = None,
    balance_tolerance_shares: float = 0.1,
) -> list[WalletSummary]:
    """Aggregate per-token activity without netting across markets."""
    direct_ctf = direct_ctf or {}
    per_market: dict[tuple[str, str], dict[int, TokenActivity]] = defaultdict(dict)
    for row in activity:
        if row.side not in (0, 1):
            raise ValueError(f"invalid side {row.side} for {row.slug}")
        key = (row.wallet.lower(), row.slug)
        if row.side in per_market[key]:
            raise ValueError(f"duplicate per-token row for {key}, side={row.side}")
        per_market[key][row.side] = row

    summaries: dict[str, WalletSummary] = {}
    for (wallet, slug), sides in per_market.items():
        summary = summaries.setdefault(wallet, WalletSummary(wallet=wallet))
        rows = list(sides.values())
        summary.market_windows += 1
        summary.pnl += sum(row.pnl for row in rows)
        summary.volume += sum(row.volume for row in rows)
        summary.sold += sum(row.sold for row in rows)
        summary.inventory_floor_shares += sum(max(0.0, row.sold - row.bought) for row in rows)
        summary.maker_volume += sum(row.maker_volume for row in rows)
        summary.taker_fees += sum(row.buy_fee + row.sell_fee for row in rows)
        summary.fills += sum(row.fills for row in rows)

        has_pair = set(sides) == {0, 1}
        summary.both_windows += int(has_pair)
        summary.bought_both_windows += int(
            has_pair and all(sides[side].bought > 0 for side in (0, 1))
        )
        summary.sold_both_windows += int(
            has_pair and all(sides[side].sold > 0 for side in (0, 1))
        )
        up_row, down_row = sides.get(1), sides.get(0)
        if up_row is not None and down_row is not None:
            if up_row.bought > 0 and down_row.bought > 0:
                paired = min(up_row.bought, down_row.bought)
                summary.paired_buy_shares += paired
                summary.paired_buy_cost += paired * (
                    (up_row.buy_usdc + up_row.buy_fee) / up_row.bought
                    + (down_row.buy_usdc + down_row.buy_fee) / down_row.bought
                )
            if up_row.sold > 0 and down_row.sold > 0:
                paired = min(up_row.sold, down_row.sold)
                summary.paired_sell_shares += paired
                summary.paired_sell_proceeds += paired * (
                    (up_row.sell_usdc - up_row.sell_fee) / up_row.sold
                    + (down_row.sell_usdc - down_row.sell_fee) / down_row.sold
                )
        net_up = up_row.net_shares if up_row is not None else 0.0
        net_down = down_row.net_shares if down_row is not None else 0.0
        imbalance = abs(net_up - net_down)
        summary.net_imbalance_sum += imbalance
        summary.balanced_end_windows += int(imbalance <= balance_tolerance_shares)

        split_sets, merge_sets = direct_ctf.get((wallet, slug), (0.0, 0.0))
        summary.direct_split_sets += split_sets
        summary.direct_merge_sets += merge_sets
    return sorted(summaries.values(), key=lambda row: row.pnl, reverse=True)
