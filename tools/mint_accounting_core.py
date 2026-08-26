"""Exact lot-allocation diagnostics for mint-funded two-sided sales."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable


class AccountingError(ValueError):
    """Observed events cannot support the requested accounting result."""


@dataclass(frozen=True)
class Sale:
    """One outcome-token sale, expressed in six-decimal base units."""

    block_number: int
    log_index: int
    side_up: bool
    shares_base: int
    cash_base: int

    def validate(self) -> None:
        if min(self.block_number, self.log_index) < 0:
            raise AccountingError("sale ordering fields must be non-negative")
        if self.shares_base <= 0 or self.cash_base < 0:
            raise AccountingError("sale amounts must be positive shares and non-negative cash")

    @property
    def price(self) -> Fraction:
        return Fraction(self.cash_base, self.shares_base)


@dataclass
class _Lot:
    shares: int
    price: Fraction


def rational(value: Fraction | int) -> dict[str, str]:
    """Serialize exact economics without decimal rounding."""
    item = Fraction(value)
    return {"numerator": str(item.numerator), "denominator": str(item.denominator)}


def _totals(sales: Iterable[Sale]) -> dict[bool, tuple[int, int]]:
    result = {True: [0, 0], False: [0, 0]}
    for sale in sales:
        sale.validate()
        result[sale.side_up][0] += sale.shares_base
        result[sale.side_up][1] += sale.cash_base
    return {side: (values[0], values[1]) for side, values in result.items()}


def _terminal_total(sales: Iterable[Sale], winner_up: bool) -> Fraction:
    rows = list(sales)
    return Fraction(sum(row.cash_base for row in rows)
                    - sum(row.shares_base for row in rows if row.side_up == winner_up))


def fifo_decomposition(sales: Iterable[Sale], winner_up: bool) -> dict[str, object]:
    """Pair chronological opposite-side sales and retain the unmatched side."""
    rows = sorted(sales, key=lambda row: (row.block_number, row.log_index))
    totals = _totals(rows)
    queues: dict[bool, deque[_Lot]] = {True: deque(), False: deque()}
    paired_shares = 0
    paired_pnl = Fraction(0)
    for sale in rows:
        remaining = sale.shares_base
        opposite = queues[not sale.side_up]
        while remaining and opposite:
            lot = opposite[0]
            matched = min(remaining, lot.shares)
            paired_shares += matched
            paired_pnl += matched * (sale.price + lot.price - 1)
            remaining -= matched
            lot.shares -= matched
            if not lot.shares:
                opposite.popleft()
        if remaining:
            queues[sale.side_up].append(_Lot(remaining, sale.price))
    open_sides = [side for side, queue in queues.items() if queue]
    if len(open_sides) > 1:
        raise AccountingError("FIFO left unmatched lots on both outcomes")
    excess_side = open_sides[0] if open_sides else None
    residual_shares = 0
    residual_cash = Fraction()
    if excess_side is not None:
        residual_shares = sum([lot.shares for lot in queues[excess_side]])
        residual_cash = sum(
            (lot.shares * lot.price for lot in queues[excess_side]), Fraction(),
        )
    retained_payoff = residual_shares if excess_side is not None and winner_up != excess_side else 0
    residual_pnl = residual_cash + retained_payoff - residual_shares
    total = paired_pnl + residual_pnl
    if paired_shares != min(totals[True][0], totals[False][0]):
        raise AccountingError("FIFO paired quantity does not reconcile")
    if total != _terminal_total(rows, winner_up):
        raise AccountingError("FIFO decomposition does not reconcile to terminal PnL")
    return {
        "method": "chronological_fifo",
        "paired_shares_base": str(paired_shares),
        "paired_pnl_base": rational(paired_pnl),
        "residual_side": None if excess_side is None else ("up" if excess_side else "down"),
        "residual_shares_base": str(residual_shares),
        "residual_sale_cash_base": rational(residual_cash),
        "residual_pnl_base": rational(residual_pnl),
        "total_pnl_base": rational(total),
    }


def proportional_decomposition(sales: Iterable[Sale], winner_up: bool) -> dict[str, object]:
    """Allocate paired quantity at each side's exact volume-weighted price."""
    rows = list(sales)
    totals = _totals(rows)
    up_shares, up_cash = totals[True]
    down_shares, down_cash = totals[False]
    paired = min(up_shares, down_shares)
    paired_cash = Fraction()
    if paired:
        paired_cash = paired * (Fraction(up_cash, up_shares) + Fraction(down_cash, down_shares))
    paired_pnl = paired_cash - paired
    excess_side = True if up_shares > down_shares else False if down_shares > up_shares else None
    residual_shares = abs(up_shares - down_shares)
    if excess_side is None:
        residual_cash = Fraction()
    else:
        side_shares, side_cash = totals[excess_side]
        residual_cash = residual_shares * Fraction(side_cash, side_shares)
    retained_payoff = residual_shares if excess_side is not None and winner_up != excess_side else 0
    residual_pnl = residual_cash + retained_payoff - residual_shares
    total = paired_pnl + residual_pnl
    if total != _terminal_total(rows, winner_up):
        raise AccountingError("proportional decomposition does not reconcile to terminal PnL")
    return {
        "method": "side_vwap_proportional",
        "paired_shares_base": str(paired),
        "paired_pnl_base": rational(paired_pnl),
        "residual_side": None if excess_side is None else ("up" if excess_side else "down"),
        "residual_shares_base": str(residual_shares),
        "residual_sale_cash_base": rational(residual_cash),
        "residual_pnl_base": rational(residual_pnl),
        "total_pnl_base": rational(total),
    }


def _selected_cash(sales: list[Sale], shares: int, highest: bool) -> Fraction:
    remaining = shares
    result = Fraction()
    for sale in sorted(sales, key=lambda row: row.price, reverse=highest):
        selected = min(remaining, sale.shares_base)
        result += selected * sale.price
        remaining -= selected
        if not remaining:
            return result
    raise AccountingError("allocation bound exceeds available excess-side shares")


def allocation_bounds(sales: Iterable[Sale], winner_up: bool) -> dict[str, object]:
    """Bound residual PnL over all price-ordered excess-fill allocations."""
    rows = list(sales)
    totals = _totals(rows)
    up_shares, _ = totals[True]
    down_shares, _ = totals[False]
    residual_shares = abs(up_shares - down_shares)
    excess_side = True if up_shares > down_shares else False if down_shares > up_shares else None
    if excess_side is None:
        low_cash = high_cash = Fraction()
    else:
        eligible = [row for row in rows if row.side_up == excess_side]
        low_cash = _selected_cash(eligible, residual_shares, highest=False)
        high_cash = _selected_cash(eligible, residual_shares, highest=True)
    retained_payoff = residual_shares if excess_side is not None and winner_up != excess_side else 0
    low_pnl = low_cash + retained_payoff - residual_shares
    high_pnl = high_cash + retained_payoff - residual_shares
    total = _terminal_total(rows, winner_up)
    return {
        "residual_side": None if excess_side is None else ("up" if excess_side else "down"),
        "residual_shares_base": str(residual_shares),
        "residual_pnl_lower_base": rational(low_pnl),
        "residual_pnl_upper_base": rational(high_pnl),
        "paired_pnl_at_lower_residual_base": rational(total - low_pnl),
        "paired_pnl_at_upper_residual_base": rational(total - high_pnl),
        "total_pnl_base": rational(total),
    }


def allocation_diagnostics(sales: Iterable[Sale], winner_up: bool) -> dict[str, object]:
    """Return the three exact, mutually reconciling allocation views."""
    rows = list(sales)
    fifo = fifo_decomposition(rows, winner_up)
    proportional = proportional_decomposition(rows, winner_up)
    bounds = allocation_bounds(rows, winner_up)
    fifo_residual = Fraction(
        int(fifo["residual_pnl_base"]["numerator"]),  # type: ignore[index]
        int(fifo["residual_pnl_base"]["denominator"]),  # type: ignore[index]
    )
    low = Fraction(
        int(bounds["residual_pnl_lower_base"]["numerator"]),  # type: ignore[index]
        int(bounds["residual_pnl_lower_base"]["denominator"]),  # type: ignore[index]
    )
    high = Fraction(
        int(bounds["residual_pnl_upper_base"]["numerator"]),  # type: ignore[index]
        int(bounds["residual_pnl_upper_base"]["denominator"]),  # type: ignore[index]
    )
    if not low <= fifo_residual <= high:
        raise AccountingError("FIFO residual lies outside admissible allocation bounds")
    return {"fifo": fifo, "proportional": proportional, "bounds": bounds}
