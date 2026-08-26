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
        "basket99", "mintwin", "mintwin_f5", "mintwin_t0",
    ]
    mint = {config.name: config for config in board if config.mode == "mint"}
    # winner-matched parameters (tools/winner_profile.py, 391 BTC windows)
    assert mint["mintwin"].sell_sum_floor == 1.00
    assert mint["mintwin"].imbalance_tolerance == 7.0
    assert mint["mintwin"].clip_shares == 6.0
    assert mint["mintwin"].new_pair_start_s == 5
    # controls isolate exactly one parameter each
    assert mint["mintwin_f5"].sell_sum_floor == 1.005
    assert mint["mintwin_f5"].imbalance_tolerance == 7.0
    assert mint["mintwin_t0"].sell_sum_floor == 1.00
    assert mint["mintwin_t0"].imbalance_tolerance == 0.1


def test_strategy_board_canonical_hash_is_stable() -> None:
    board = current_strategy_board(0.065)

    assert " " not in canonical_board(board)
    assert strategy_board_hash(board) == (
        "4a15891298dc6d4a56fbcbe37d2b3cecafa50b52a571749b6da2f0a47f00ea9c"
    )


def test_execution_model_identity_has_an_explicit_boundary() -> None:
    identity = execution_model_identity()

    assert identity["schema"] == "project-fail-paper-model-v2"
    assert identity["source_count"] == 25
