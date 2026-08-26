#!/usr/bin/env python3
"""Evaluate the frozen mint-sibling falsification gate with integer arithmetic."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from tools.mint_accounting_inputs import EvidenceError


GATE_SPEC: dict[str, object] = {
    "endpoint": "contractual_terminal_pnl_base_ex_rebate",
    "statistical_unit": "market_window_sum_across_frozen_10_wallets",
    "wallets": 10,
    "windows": 31,
    "wallet_windows": 310,
    "required_positive_wallets": 7,
    "positive_definition": "strictly_greater_than_zero",
    "minimum_t_statistic": "2",
    "zero_variance_policy": "non_passing",
    "rebate_policy": "forbidden_in_primary_falsification",
    "incomplete_ledger_policy": (
        "inconclusive_no_result_never_drop_or_replace_wallets"
    ),
    "pass_authorization": "one_prospective_causal_paper_design_only",
}


def validated_gate_spec(value: object) -> dict[str, object]:
    """Require the exact frozen gate contract, with no defaults or coercion."""
    if not isinstance(value, Mapping) or dict(value) != GATE_SPEC:
        raise EvidenceError("cohort falsification gate spec differs from the frozen contract")
    return dict(GATE_SPEC)


def evaluate_gate(
    gate_spec: object,
    wallet_terminal_pnl_base: Sequence[int],
    window_terminal_pnl_base: Sequence[int],
) -> dict[str, object]:
    """Return the deterministic preregistered decision over the complete fixed grid."""
    spec = validated_gate_spec(gate_spec)
    required_wallets = cast(int, spec["wallets"])
    required_windows = cast(int, spec["windows"])
    if (len(wallet_terminal_pnl_base) != required_wallets
            or len(window_terminal_pnl_base) != required_windows):
        raise EvidenceError("falsification gate requires the exact frozen 10x31 grid")
    if any(isinstance(value, bool) or not isinstance(value, int)
           for value in (*wallet_terminal_pnl_base, *window_terminal_pnl_base)):
        raise EvidenceError("falsification gate endpoints must be exact signed integers")

    positive_wallets = sum(value > 0 for value in wallet_terminal_pnl_base)
    required_positive = cast(int, spec["required_positive_wallets"])
    wallet_pass = positive_wallets >= required_positive

    n = len(window_terminal_pnl_base)
    total = sum(window_terminal_pnl_base)
    sum_squares = sum(value * value for value in window_terminal_pnl_base)
    variance_numerator = n * sum_squares - total * total
    if variance_numerator < 0:
        raise EvidenceError("falsification gate variance numerator is negative")
    comparison_lhs = (n - 1) * total * total
    comparison_rhs = 4 * variance_numerator
    mean_positive = total > 0
    finite_positive_variance = variance_numerator > 0
    t_pass = (
        mean_positive
        and finite_positive_variance
        and comparison_lhs >= comparison_rhs
    )
    passed = wallet_pass and t_pass

    return {
        "spec": spec,
        "wallet_gate": {
            "positive_wallets": positive_wallets,
            "required_positive_wallets": required_positive,
            "strictly_positive": True,
            "pass": wallet_pass,
        },
        "market_window_gate": {
            "n": n,
            "sum_x_base": str(total),
            "sum_x2_base2": str(sum_squares),
            "variance_numerator_n_sum_x2_minus_sum_x_squared_base2": str(
                variance_numerator
            ),
            "t_squared_numerator": str(comparison_lhs),
            "t_squared_denominator": str(variance_numerator),
            "comparison_lhs": str(comparison_lhs),
            "comparison_rhs": str(comparison_rhs),
            "minimum_t_statistic": str(spec["minimum_t_statistic"]),
            "mean_positive": mean_positive,
            "finite_positive_variance": finite_positive_variance,
            "t_at_least_minimum_exact": t_pass,
            "pass": t_pass,
        },
        "incomplete_ledger_policy": spec["incomplete_ledger_policy"],
        "pass": passed,
        "verdict": (
            "survives_pre_registered_gates"
            if passed else "fails_pre_registered_gates"
        ),
        "authorization": spec["pass_authorization"] if passed else "none",
    }
