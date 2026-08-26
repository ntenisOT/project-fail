from __future__ import annotations

from paper.pair_types import PairConfig
from paper.strategy_board import (
    canonical_board,
    current_strategy_board,
    execution_model_identity,
    strategy_board_hash,
)


def test_current_strategy_board_is_the_focused_fill_probe() -> None:
    latency = 0.065
    expected = (
        PairConfig(
            "basket99", "accumulate", 0.02,
            action_latency_s=latency, buy_sum_ceiling=0.99, improve_ticks=1,
            require_both_to_start=True, basket_average_cap=True,
            new_pair_start_s=30,
        ),
    )

    board = current_strategy_board(latency)
    assert board[0] == expected[0], "basket99 control must stay byte-identical"
    assert [config.name for config in board] == [
        "basket99", "basket97", "basket95", "mom10", "mom05",
    ]
    ceilings = {c.name: c.buy_sum_ceiling for c in board if c.mode == "accumulate"}
    assert ceilings == {"basket99": 0.99, "basket97": 0.97, "basket95": 0.95}
    # the selective arms must rest below the book, not refuse to quote
    patient = {c.name: c.patient_bids for c in board if c.mode == "accumulate"}
    assert patient == {"basket99": False, "basket97": True, "basket95": True}
    for config in board:
        if config.mode == "accumulate":
            assert config.new_pair_start_s == 30
            assert config.basket_average_cap
    momentum = {c.name: c for c in board if c.mode == "momentum"}
    assert momentum["mom10"].momentum_threshold == 0.10
    assert momentum["mom05"].momentum_threshold == 0.05
    for config in momentum.values():
        assert config.momentum_lookback_s == 10.0
        assert config.momentum_hold_s == 30.0




def test_strategy_board_canonical_hash_is_stable() -> None:
    board = current_strategy_board(0.065)

    assert " " not in canonical_board(board)
    assert strategy_board_hash(board) == (
        "d4e3f85382c0949df167026c9216f5b86b8641d4483861fe935aec8441b01c87"
    )


def test_execution_model_identity_has_an_explicit_boundary() -> None:
    identity = execution_model_identity()

    assert identity["schema"] == "project-fail-paper-model-v2"
    assert identity["source_count"] == 25
