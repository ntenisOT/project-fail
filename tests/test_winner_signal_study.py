from __future__ import annotations

import datetime as dt
import math
import pathlib

import pytest

from tools.binance_history import SecondBar
from tools.market_windows import ResolvedWindow
from tools.wallet_pairs import BuyFill
from tools.winner_signal_metrics import (
    BlockAction,
    aggregate_block_actions,
    lagged_return_bp,
    summarize_association,
)
from tools.winner_signal_study import (
    CAVEAT,
    FrozenStudy,
    _archive_specs,
    bounded_analysis_end,
    build_report,
)


WALLET = "0x" + "a" * 40
START = 1_787_691_000
SLUG = f"btc-updown-5m-{START}"


def _window(start: int = START, winner_up: int = 1) -> ResolvedWindow:
    return ResolvedWindow(
        f"btc-updown-5m-{start}", "btc", start, "0x" + "1" * 64,
        "11", "22", winner_up,
    )


def _bar(ts: int, close: float) -> SecondBar:
    return SecondBar(ts, close, close, close, close, 1, 0.5, 1)


def test_block_aggregation_filters_event_and_separates_roles_and_markout() -> None:
    window = _window()
    fills = [
        BuyFill(WALLET, SLUG, 1, (10, 1), START + 10, 10, 0.4, True),
        BuyFill(WALLET, SLUG, 0, (10, 2), START + 10, 6, 0.5, True),
        BuyFill(WALLET, SLUG, 0, (10, 3), START + 10, 5, 0.55, False, 0.05),
        BuyFill(WALLET, SLUG, 1, (9, 1), START - 1, 99, 0.1, True),
    ]

    rows, counts = aggregate_block_actions(fills, {SLUG: window}, START + 300)

    assert len(rows) == 2
    maker = next(row for row in rows if row.role == "maker")
    taker = next(row for row in rows if row.role == "taker")
    assert (maker.up_shares, maker.down_shares, maker.fills) == (10, 6, 2)
    assert maker.terminal_markout == pytest.approx(3)
    assert maker.neutral_pair_markout == pytest.approx(0.6)
    assert maker.directional_markout == pytest.approx(2.4)
    assert taker.terminal_markout == pytest.approx(-2.8)
    assert counts["outside_event_window_buy_fills"] == 1


def test_lagged_return_uses_only_complete_bars_before_margin() -> None:
    bars = {
        84: _bar(84, 100),
        94: _bar(94, 110),
        95: _bar(95, 1_000_000),
        100: _bar(100, 2_000_000),
    }

    result = lagged_return_bp(bars, block_ts=100, margin_s=5, horizon_s=10)

    assert result == pytest.approx(10_000 * math.log(1.1))
    assert lagged_return_bp(bars, 100, 5, 5) is None


def test_association_reports_alignment_markout_and_independent_counts() -> None:
    second_start = START + 86_400
    actions = [
        BlockAction(
            "holdout", WALLET, SLUG, START, 1, START + 100, "maker", True,
            1, 0, 0.4, 0, 1,
        ),
        BlockAction(
            "holdout", WALLET, f"btc-updown-5m-{second_start}", second_start,
            2, second_start + 100, "maker", False, 1, 0, 0.4, 0, 1,
        ),
    ]
    bars = {
        START + 89: _bar(START + 89, 100),
        START + 94: _bar(START + 94, 101),
        second_start + 89: _bar(second_start + 89, 101),
        second_start + 94: _bar(second_start + 94, 100),
    }

    row = summarize_association(
        actions, bars, split="holdout", role="maker", source="spot",
        margin_s=5, horizon_s=5,
    )

    assert row["aligned_groups"] == 1
    assert row["opposed_groups"] == 1
    assert row["alignment_rate"] == 0.5
    assert row["winner_alignment_rate"] == 0.5
    assert row["feature_winner_alignment_rate"] == 1.0
    assert row["aligned_directional_markout_usd"] == 0.6
    assert row["opposed_directional_markout_usd"] == -0.4
    assert (row["wallets"], row["windows"], row["utc_days"]) == (1, 2, 2)
    assert row["claim_level"] == "exploratory_descriptive_only"
    assert row["cluster_counts_sufficient_for_inference"] is False


def test_bounded_analysis_end_preserves_frozen_split_and_rejects_drift() -> None:
    study = FrozenStudy(300, 1_500, 600, 900, (WALLET,))

    assert bounded_analysis_end(study, None) == 1_500
    assert bounded_analysis_end(study, 1_200) == 1_200
    with pytest.raises(ValueError, match="holdout"):
        bounded_analysis_end(study, 600)
    with pytest.raises(ValueError, match="exceeds"):
        bounded_analysis_end(study, 1_800)


def test_archive_specs_do_not_request_next_day_at_final_midnight() -> None:
    end = int(dt.datetime(2026, 8, 24, 23, 55, tzinfo=dt.UTC).timestamp())

    specs = _archive_specs(end - 300, end)

    assert {spec.date.isoformat() for spec in specs} == {"2026-08-24"}


def test_frozen_study_requires_every_boundary_to_be_five_minute_aligned() -> None:
    valid = [300, 1_500, 600, 900]
    for index in range(4):
        boundaries = valid.copy()
        boundaries[index] += 1
        with pytest.raises(ValueError, match="five-minute aligned"):
            FrozenStudy(
                boundaries[0], boundaries[1], boundaries[2], boundaries[3], (WALLET,)
            )


@pytest.mark.parametrize("window_start", (0, 1_500))
def test_report_rejects_actions_outside_its_bounded_period(window_start: int) -> None:
    action = BlockAction(
        "holdout", WALLET, "slug", window_start, 1, window_start + 1,
        "maker", True, 1, 0, 0.4, 0, 1,
    )
    with pytest.raises(ValueError, match="outside the bounded analysis period"):
        build_report(
            FrozenStudy(300, 1_500, 600, 900, (WALLET,)), [action],
            {"spot": {}, "futures": {}}, analysis_end=1_200,
            lifecycle_path=pathlib.Path("unused"),
            window_cache=pathlib.Path("unused"), archive_manifest=[], fill_counts={},
        )


def test_report_labels_grid_holdout_and_markouts_as_exploratory(tmp_path) -> None:
    lifecycle, cache = tmp_path / "lifecycle.json", tmp_path / "windows.jsonl"
    lifecycle.write_text("{}", encoding="utf-8")
    cache.write_text("{}", encoding="utf-8")
    report = build_report(
        FrozenStudy(300, 1_500, 600, 900, (WALLET,)), [],
        {"spot": {}, "futures": {}}, analysis_end=1_200,
        lifecycle_path=lifecycle, window_cache=cache,
        archive_manifest=[], fill_counts={},
    )
    parameters = report["parameters"]

    assert isinstance(parameters, dict)
    assert parameters["specification_grid"] == {
        "status": "exploratory", "cells_per_split": 36,
        "reported_splits": 2, "correlated": True,
        "selection_status": "no specification or threshold validated",
    }
    holdout = parameters["holdout_evaluation"]
    assert isinstance(holdout, dict)
    assert holdout["status"] == "previously_observable_non_prospective"
    assert str(parameters["economics"]).startswith("buy-fill terminal markouts only")
    assert report["caveat"] == CAVEAT
    assert "no strategy is validated" in CAVEAT
