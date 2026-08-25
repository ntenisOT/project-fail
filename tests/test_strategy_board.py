from __future__ import annotations

from paper.pair_types import PairConfig
from paper.strategy_board import (
    canonical_board,
    current_strategy_board,
    strategy_board_hash,
)


def test_current_strategy_board_is_the_focused_two_control_board() -> None:
    latency = 0.065
    expected = (
        PairConfig(
            "basket99", "accumulate", 0.02,
            action_latency_s=latency, buy_sum_ceiling=0.99, improve_ticks=1,
            require_both_to_start=True, basket_average_cap=True,
            new_pair_start_s=30,
        ),
        PairConfig(
            "mintcycle5", "mint", 0.5,
            action_latency_s=latency, mint_sets=5, sell_sum_floor=1.005,
            new_pair_start_s=30, new_pair_cutoff_s=240,
            mint_anchor_spread=0.02,
        ),
    )

    assert current_strategy_board(latency) == expected


def test_strategy_board_canonical_hash_is_stable() -> None:
    board = current_strategy_board(0.065)

    assert " " not in canonical_board(board)
    assert strategy_board_hash(board) == (
        "85f335bf649bea7d7960507b164bfc5bb08b233b0067261efa46a84e603f2a77"
    )
