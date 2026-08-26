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
        "basket99", "basket97", "basket95",
    ]
    ceilings = {config.name: config.buy_sum_ceiling for config in board}
    assert ceilings == {"basket99": 0.99, "basket97": 0.97, "basket95": 0.95}
    for config in board:
        assert config.mode == "accumulate"
        assert config.new_pair_start_s == 30
        assert config.basket_average_cap




def test_strategy_board_canonical_hash_is_stable() -> None:
    board = current_strategy_board(0.065)

    assert " " not in canonical_board(board)
    assert strategy_board_hash(board) == (
        "38a89bf7d46696055254433c9a76040e42da5a8d5f5fa8c1b3ffb352f926f741"
    )


def test_execution_model_identity_has_an_explicit_boundary() -> None:
    identity = execution_model_identity()

    assert identity["schema"] == "project-fail-paper-model-v2"
    assert identity["source_count"] == 25
