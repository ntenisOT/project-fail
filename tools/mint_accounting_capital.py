"""Exact cross-window capital diagnostics for observed mint-seller events."""

from __future__ import annotations

from typing import Mapping, Sequence

from tools.mint_accounting_inputs import EvidenceError, integer, signed_integer


def _event_delta(event: Mapping[str, object]) -> int:
    kind = str(event.get("type") or "")
    if kind == "split":
        return -integer(event.get("amount_base"), "split amount")
    if kind == "merge":
        return integer(event.get("amount_base"), "merge amount")
    if kind == "maker_sell":
        return integer(event.get("cash_delta_base"), "sale cash")
    raise EvidenceError(f"unsupported capital event: {kind!r}")


def _no_merge_tail(window: Mapping[str, object], t1: int) -> dict[str, object]:
    up = integer(window.get("ending_up_base"), "ending up")
    down = integer(window.get("ending_down_base"), "ending down")
    collateral_equivalent = signed_integer(
        window.get("ledger_collateral_equivalent_base"), "ledger collateral equivalent"
    )
    mergeable = min(up, down)
    residual = abs(up - down)
    residual_side = "up" if up > down else "down" if down > up else None
    winner = str(window.get("winner") or "")
    payoff = residual if residual_side == winner else 0
    floor = collateral_equivalent + mergeable
    terminal = floor + payoff
    if floor != signed_integer(
        window.get("contractual_pair_recovery_residual_zero_floor_pnl_base"),
        "residual floor",
    ):
        raise EvidenceError("no-merge tail does not reconcile to its residual floor")
    if terminal != signed_integer(window.get("contractual_terminal_pnl_base"), "terminal"):
        raise EvidenceError("no-merge tail does not reconcile to terminal PnL")
    start = integer(window.get("start"), "window start")
    return {
        "condition_id": str(window.get("condition_id") or ""),
        "slug": str(window.get("slug") or ""),
        "last_maker_sell_timestamp": window.get("last_maker_sell_timestamp"),
        "market_close_timestamp": start + 300,
        "tail_boundary_timestamp": t1,
        "contractual_mergeable_pair_base": str(mergeable),
        "ending_up_base": str(up),
        "ending_down_base": str(down),
        "unmatched_up_base": str(max(0, up - mergeable)),
        "unmatched_down_base": str(max(0, down - mergeable)),
        "residual_side": residual_side,
        "residual_shares_base": str(residual),
        "unmatched_residual_zero_value_base": "0",
        "residual_terminal_payoff_base": str(payoff),
        "mechanics_implied_collateral_requirement_base": str(
            max(0, -collateral_equivalent)
        ),
        "unmerged_after_close_s": max(0, t1 - (start + 300)),
    }


def capital_diagnostics(
    windows: Sequence[Mapping[str, object]],
    events: Sequence[Mapping[str, object]],
    t0: int,
    t1: int,
    rebate_base: int | None,
) -> dict[str, object]:
    """Integrate mechanics-implied collateral requirements without backdating."""
    if t0 < 0 or t1 <= t0:
        raise EvidenceError("invalid capital integration interval")
    ordered = sorted(
        events,
        key=lambda row: (
            integer(row.get("block_number"), "event block"),
            integer(row.get("log_index"), "event log"),
        ),
    )
    seen: set[tuple[int, int]] = set()
    active: dict[str, int] = {}
    window_collateral_equivalent = {
        str(window.get("condition_id") or "").lower(): 0
        for window in windows
    }
    if (not window_collateral_equivalent or "" in window_collateral_equivalent
            or len(window_collateral_equivalent) != len(windows)):
        raise EvidenceError("capital windows need unique nonempty conditions")
    collateral_equivalent = 0
    portfolio_peak_draw = 0
    non_cross_peak_deficit = 0
    peak_open_count = 0
    peak_open_principal = 0
    portfolio_draw_seconds = 0
    non_cross_deficit_seconds = 0
    split_principal_seconds = 0
    state_path: list[dict[str, object]] = []
    previous_at = t0
    for event in ordered:
        block = integer(event.get("block_number"), "event block")
        log = integer(event.get("log_index"), "event log")
        if (block, log) in seen:
            raise EvidenceError("duplicate global capital event block/log key")
        seen.add((block, log))
        observed_at = integer(event.get("timestamp"), "event timestamp")
        if not previous_at <= observed_at < t1:
            raise EvidenceError("capital events are not monotone inside the lifecycle interval")
        elapsed = observed_at - previous_at
        portfolio_draw_seconds += max(0, -collateral_equivalent) * elapsed
        non_cross_deficit_seconds += sum(
            max(0, -value) for value in window_collateral_equivalent.values()
        ) * elapsed
        split_principal_seconds += sum(active.values()) * elapsed
        condition = str(event.get("condition_id") or "").lower()
        if condition not in window_collateral_equivalent:
            raise EvidenceError("capital event condition escapes the frozen windows")
        kind = str(event.get("type") or "")
        if kind == "split":
            if not condition or condition in active:
                raise EvidenceError("split principal opened twice or lacks a condition")
            active[condition] = integer(event.get("amount_base"), "split amount")
        elif kind == "merge":
            if condition not in active:
                raise EvidenceError("merge closes no active split principal")
            del active[condition]
        delta = _event_delta(event)
        collateral_equivalent += delta
        window_collateral_equivalent[condition] += delta
        portfolio_peak_draw = max(portfolio_peak_draw, -collateral_equivalent)
        non_cross_peak_deficit = max(
            non_cross_peak_deficit,
            sum(max(0, -value) for value in window_collateral_equivalent.values()),
        )
        peak_open_count = max(peak_open_count, len(active))
        peak_open_principal = max(peak_open_principal, sum(active.values()))
        state_path.append({
            "block_number": block,
            "log_index": log,
            "timestamp": observed_at,
            "condition_id": condition,
            "type": kind,
            "portfolio_ledger_collateral_equivalent_after_base": str(
                collateral_equivalent
            ),
            "portfolio_mechanics_implied_collateral_requirement_after_base": str(
                max(0, -collateral_equivalent)
            ),
            "non_cross_netted_mechanics_implied_collateral_requirement_after_base": str(
                sum(max(0, -value) for value in window_collateral_equivalent.values())
            ),
            "gross_open_split_count_after": len(active),
            "gross_open_split_principal_after_base": str(sum(active.values())),
        })
        previous_at = observed_at
    elapsed = t1 - previous_at
    portfolio_draw_seconds += max(0, -collateral_equivalent) * elapsed
    non_cross_deficit_seconds += sum(
        max(0, -value) for value in window_collateral_equivalent.values()
    ) * elapsed
    split_principal_seconds += sum(active.values()) * elapsed
    expected_by_condition = {
        str(window.get("condition_id") or "").lower(): signed_integer(
            window.get("ledger_collateral_equivalent_base"),
            "window ledger collateral equivalent",
        )
        for window in windows
    }
    if window_collateral_equivalent != expected_by_condition:
        raise EvidenceError(
            "cross-window collateral-equivalent paths do not reconcile to every window ledger"
        )
    expected_collateral_equivalent = sum(expected_by_condition.values())
    if collateral_equivalent != expected_collateral_equivalent:
        raise EvidenceError(
            "cross-window collateral-equivalent total does not reconcile to window ledgers"
        )
    no_merge = [
        _no_merge_tail(window, t1)
        for window in windows
        if integer(window.get("merge_base"), "merge") == 0
    ]
    if len(active) != len(no_merge):
        raise EvidenceError("open split principals do not match no-merge windows")
    return {
        "basis": "mechanics_implied_collateral_equivalent_and_gross_split_lifecycle",
        "portfolio_peak_mechanics_implied_collateral_requirement_base": str(
            max(0, portfolio_peak_draw)
        ),
        "portfolio_mechanics_implied_collateral_requirement_seconds_base_seconds": str(
            portfolio_draw_seconds
        ),
        "non_cross_netted_peak_mechanics_implied_collateral_requirement_base": str(
            max(0, non_cross_peak_deficit)
        ),
        "non_cross_netted_mechanics_implied_collateral_requirement_seconds_base_seconds": str(
            non_cross_deficit_seconds
        ),
        "ledger_collateral_equivalent_at_end_base": str(collateral_equivalent),
        "non_cross_netted_mechanics_implied_collateral_requirement_at_end_base": str(
            sum(max(0, -value) for value in window_collateral_equivalent.values())
        ),
        "peak_open_split_count": peak_open_count,
        "peak_gross_open_split_principal_base": str(peak_open_principal),
        "gross_split_principal_seconds_base_seconds": str(split_principal_seconds),
        "open_split_count_at_end": len(active),
        "gross_open_split_principal_at_end_base": str(sum(active.values())),
        "state_path": state_path,
        "rebate_endpoint_base": None if rebate_base is None else str(rebate_base),
        "rebate_recycled_inside_scope_base": "0",
        "rebate_availability_observed_at": None,
        "rebate_payment_finality": False,
        "rebate_reuse_policy": "excluded_from_collateral_equivalent_and_capital_estimate_paths",
        "no_merge_tails": no_merge,
    }
