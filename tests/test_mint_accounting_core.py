from fractions import Fraction

import pytest

from tools.mint_accounting_core import (
    AccountingError,
    Sale,
    allocation_diagnostics,
    fifo_decomposition,
)


def _fraction(value: object) -> Fraction:
    assert isinstance(value, dict)
    return Fraction(int(value["numerator"]), int(value["denominator"]))


def test_fifo_exactly_reconciles_partial_lots_and_terminal_payoff() -> None:
    sales = [
        Sale(1, 1, True, 6, 4),
        Sale(2, 1, False, 4, 2),
    ]
    result = fifo_decomposition(sales, winner_up=False)
    assert result["paired_shares_base"] == "4"
    assert result["residual_side"] == "up"
    assert _fraction(result["paired_pnl_base"]) == Fraction(2, 3)
    assert _fraction(result["residual_pnl_base"]) == Fraction(4, 3)
    assert _fraction(result["total_pnl_base"]) == 2


def test_allocation_views_share_total_and_fifo_stays_inside_bounds() -> None:
    sales = [
        Sale(1, 1, True, 5, 1),
        Sale(2, 1, True, 5, 4),
        Sale(3, 1, False, 6, 3),
    ]
    result = allocation_diagnostics(sales, winner_up=True)
    fifo = result["fifo"]
    proportional = result["proportional"]
    bounds = result["bounds"]
    assert _fraction(fifo["total_pnl_base"]) == -2
    assert _fraction(proportional["total_pnl_base"]) == -2
    assert _fraction(bounds["total_pnl_base"]) == -2
    assert (_fraction(bounds["residual_pnl_lower_base"])
            <= _fraction(fifo["residual_pnl_base"])
            <= _fraction(bounds["residual_pnl_upper_base"]))


def test_invalid_sale_is_rejected_fail_closed() -> None:
    with pytest.raises(AccountingError, match="positive shares"):
        fifo_decomposition([Sale(1, 1, True, 0, 0)], winner_up=True)
