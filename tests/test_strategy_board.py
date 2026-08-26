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
        "basket99", "basket99f285", "basket99f240", "basket99f180",
    ]
    assert not [c for c in board if c.mode == "momentum"], (
        "momentum retired: +$5.18 gross against $26.93 of taker fees")

    # This board tests exactly one variable: when the naked residual is sold.
    # Every other field must be identical to the control, or the arms are not
    # measuring the flatten.
    flatten = {c.name: c.flatten_residual_s for c in board}
    assert flatten == {"basket99": None, "basket99f285": 285.0,
                       "basket99f240": 240.0, "basket99f180": 180.0}
    control = board[0]
    for config in board[1:]:
        differing = {
            field for field in vars(control)
            if getattr(control, field) != getattr(config, field)
        }
        assert differing == {"name", "flatten_residual_s"}, (
            f"{config.name} differs from the control in {differing}; the "
            "flatten experiment is only interpretable if nothing else moves")
    for config in board:
        assert config.mode == "accumulate"
        assert config.buy_sum_ceiling == 0.99
        assert config.new_pair_start_s == 30
        assert config.basket_average_cap



def test_strategy_board_canonical_hash_is_stable() -> None:
    board = current_strategy_board(0.065)

    assert " " not in canonical_board(board)
    assert strategy_board_hash(board) == (
        "73473bec6c4d8ed4f4278bc4502969e82d35977e89f971e1418cd0ca5c54c620"
    )


def test_execution_model_identity_has_an_explicit_boundary() -> None:
    identity = execution_model_identity()

    assert identity["schema"] == "project-fail-paper-model-v2"
    assert identity["source_count"] == 25
