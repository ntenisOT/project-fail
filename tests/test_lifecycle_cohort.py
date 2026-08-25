from __future__ import annotations

from tools.lifecycle_cohort import (
    auc,
    load_frozen_cohort,
    spearman,
    summarize_lifecycles,
)
from tools.market_windows import ResolvedWindow
from tools.wallet_metrics import TokenActivity
from tools.wallet_pairs import BuyFill


WALLET = "0x" + "1" * 40


def activity(slug: str, side: int, *, pnl: float, volume: float,
             bought: float, net: float) -> TokenActivity:
    return TokenActivity(
        WALLET, slug, "btc", int(slug.rsplit("-", 1)[1]), side,
        pnl, volume, bought, volume, 0, 0, net, volume, 1, 1,
    )


def window(start: int, winner_up: int) -> ResolvedWindow:
    return ResolvedWindow(
        f"btc-updown-5m-{start}", "btc", start, "0x" + "a" * 64,
        str(start * 10 + 1), str(start * 10 + 2), winner_up,
    )


def test_lifecycle_separates_fifo_neutral_direction_and_residual() -> None:
    first, second = window(300, 1), window(600, 1)
    rows = [
        activity(first.slug, 1, pnl=4, volume=6, bought=10, net=10),
        activity(first.slug, 0, pnl=-3, volume=3, bought=10, net=10),
        activity(second.slug, 1, pnl=3, volume=2, bought=5, net=5),
    ]
    fills = [
        BuyFill(WALLET, first.slug, 1, (1, 1), 301, 10, 0.6, True),
        BuyFill(WALLET, first.slug, 0, (1, 2), 303, 10, 0.3, True),
        BuyFill(WALLET, second.slug, 1, (2, 1), 601, 5, 0.4, True),
    ]

    result = summarize_lifecycles(
        rows, fills, [first, second], {(WALLET, first.slug): (0, 5)}, [WALLET],
    )[0]

    assert result.actual_pnl == 4
    assert result.neutral_pnl == 1.5
    assert result.directional_pnl == 2.5
    assert result.fifo_pair_sum == 0.9
    assert result.pair_completion_pct == 80
    assert result.residual_markets == 1
    assert result.residual_weighted_hit_pct == 100
    assert result.direct_merge_sets == 5
    assert result.classification == "merge_recycler"


def test_auc_handles_ties_and_requires_both_classes() -> None:
    assert auc([0.9, 0.8, 0.8], [True, True, False]) == 0.75
    assert auc([0.9], [True]) is None


def test_spearman_is_tie_aware_and_rejects_degenerate_inputs() -> None:
    assert spearman([1, 2, 3], [3, 2, 1]) == -1
    assert spearman([1, 1, 2], [2, 2, 1]) == -1
    assert spearman([1], [1]) is None
    assert spearman([1, 1], [2, 3]) is None


def test_frozen_cohort_preserves_wallet_order_and_provenance(tmp_path) -> None:
    cohort = tmp_path / "cohort.json"
    cohort.write_text(
        '{"wallets":["0xAA","0xBB"],'
        '"selection_sources":{"0xaa":["volume"],"0xbb":["merge"]}}'
    )

    wallets, sources = load_frozen_cohort(cohort)

    assert wallets == ["0xaa", "0xbb"]
    assert sources == {"0xaa": {"volume"}, "0xbb": {"merge"}}
