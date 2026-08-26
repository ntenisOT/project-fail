from __future__ import annotations

import dataclasses

import pytest

from paper.cohort_engine import CohortEngine, FillRecord
from paper.market_metadata import ActiveMarket
from paper.replay import ReplayResult
from paper.taker import crypto_fee
from tools.pair_completion_counterfactual import (
    CANDIDATE,
    _summary,
    completion_counterfactual_board,
)


def test_completion_candidate_changes_only_name_and_taker_timing() -> None:
    baseline, candidate = completion_counterfactual_board(0.065)

    assert candidate.name == CANDIDATE
    assert candidate.buy_taker_after_s == 0.0
    assert candidate.buy_sum_ceiling == 0.99
    assert dataclasses.replace(
        candidate, name=baseline.name, buy_taker_after_s=baseline.buy_taker_after_s,
    ) == baseline


@pytest.mark.parametrize("down_ask,expected_taker", ((0.49, True), (0.50, False)))
def test_completion_waits_for_latency_and_enforces_fee_inclusive_cap(
    down_ask: float, expected_taker: bool,
) -> None:
    board = completion_counterfactual_board(0.065)
    engine = CohortEngine(board)
    market = ActiveMarket(
        "btc", "btc-updown-5m-0", 0, "condition", "up", "down", 5,
    )
    engine.open_market(market, 0)
    for token, bid, ask in (
        ("up", 0.47, 0.50), ("down", 0.47, down_ask),
    ):
        engine.on_event({
            "event_type": "book", "asset_id": token, "timestamp": 1,
            "bids": [{"price": bid, "size": 1}],
            "asks": [{"price": ask, "size": 5}],
        }, 1)
    engine.tick(1)
    engine.tick(30)
    engine.tick(30.066)
    maker_fills = engine.on_event({
        "event_type": "last_trade_price", "asset_id": "up",
        "timestamp": 30.067, "side": "SELL", "price": 0.48, "size": 5,
    }, 30.067)

    assert {row.strategy for row in maker_fills} == {"basket99", CANDIDATE}
    assert engine.tick(30.131) == ()
    taker_fills = engine.tick(30.133)
    candidate_takers = [
        row for row in taker_fills
        if row.strategy == CANDIDATE and row.action == "taker_buy"
    ]

    assert bool(candidate_takers) is expected_taker
    fee_inclusive_pair = 0.48 + down_ask + crypto_fee(down_ask, 5) / 5
    assert (fee_inclusive_pair <= 0.99) is expected_taker
    if candidate_takers:
        assert candidate_takers[0].signed_cash == pytest.approx(
            -(5 * down_ask + crypto_fee(down_ask, 5))
        )


def test_unterminated_floor_never_cross_nets_distinct_markets() -> None:
    records = (
        FillRecord(1, "probe", "btc", "market-a", "buy", 0.6, 5, -3, 1),
        FillRecord(2, "probe", "btc", "market-b", "buy", 0.4, 5, -2, 0),
    )
    replay = ReplayResult(
        records, "capture", "dataset", 0, 0, 0, 0,
        "captured-board", "replay-board", "captured-model", "replay-model",
        2, 0, 0, 2, 0, 0, 0,
    )

    summary = _summary(replay, "probe")

    assert summary["unterminated_paired_shares"] == 0
    assert summary["unterminated_unmatched_shares"] == 10
    assert summary["unterminated_adverse_floor_usd"] == -5
