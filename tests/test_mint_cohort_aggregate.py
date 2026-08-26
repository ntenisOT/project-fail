from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

import pytest

from tools.mint_accounting_inputs import EvidenceError, SOURCE_PATHS, canonical, sha256
from tools.mint_cohort_aggregate import aggregate
from tools.mint_falsification_gate import GATE_SPEC


WALLETS = ["0x" + f"{index:040x}" for index in range(1, 11)]


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _revision() -> dict[str, object]:
    frozen = {
        "git_head": "a" * 40,
        "source_sha256": {name: "b" * 64 for name in SOURCE_PATHS},
        "runtime": {
            "python_implementation": "cpython", "python_version": "3.14.2",
            "clickhouse_connect_version": "0.15.1", "eth_utils_version": "6.0.0",
        },
    }
    return {**frozen, "revision_sha256": sha256(canonical(frozen))}


def _fixture(
    terminal_for: Callable[[int, int], int] | None = None,
) -> tuple[dict[str, object], list[tuple[str, dict[str, object], str]]]:
    endpoint = terminal_for or (
        lambda wallet_index, window_index: 1 + int(wallet_index == window_index == 0)
    )
    revision = _revision()
    universe = [
        {
            "asset": "btc", "slug": f"btc-updown-5m-{start}", "start": start,
            "condition_id": "0x" + f"{index:064x}",
            "up_token": str(100 + index), "down_token": str(200 + index),
        }
        for index, start in enumerate(range(300, 9_600, 300), 1)
    ]
    sources = {
        "candidate": "c" * 64,
        "attribution": "d" * 64,
        "receipt_cache": "e" * 64,
    }
    cohort: dict[str, object] = {
        "schema": "project-fail-mint-sibling-cohort-proof-v2",
        "revision": revision,
        "gate_spec": deepcopy(GATE_SPEC),
        "wallets": WALLETS,
        "outcome_free_universe": universe,
        "counts": {"wallets": 10, "windows": 31, "wallet_windows": 310},
        "accounting_lifecycle": {
            "lifecycle_start": 100,
            "lifecycle_end_exclusive": 96_000,
            "post_close_tail_s": 86_400,
            "source_watermark_unix_s": {
                "splits_merges": 96_000,
                "trade_history": 96_000,
            },
        },
        "source_sha256": sources,
    }
    ledgers: list[tuple[str, dict[str, object], str]] = []
    for wallet_index, wallet in enumerate(WALLETS):
        windows: list[dict[str, object]] = []
        for window_index, market in enumerate(universe):
            terminal = endpoint(wallet_index, window_index)
            windows.append({
                "condition_id": market["condition_id"], "slug": market["slug"],
                "start": market["start"], "winner": "up", "split_base": "100",
                "merge_base": "0", "maker_sell_count": 2,
                "sold_up_base": "100", "sold_down_base": "100",
                "sale_cash_base": str(100 + terminal),
                "contractual_terminal_pnl_base": str(terminal),
                "contractual_pair_recovery_residual_zero_floor_pnl_base": str(terminal),
                "rebate_endpoint_base": None,
            })
        terminal_total = sum(
            int(str(row["contractual_terminal_pnl_base"])) for row in windows
        )
        ledger: dict[str, object] = {
            "schema": "project-fail-mint-observed-accounting-v2",
            "revision": revision,
            "scope": {
                "wallet": wallet,
                "chain_id": 137,
                "lifecycle_start": 100,
                "lifecycle_end_exclusive": 96_000,
                "complete_wallet": False,
                "cash_realized": False,
                "collateral_cash_path_observed": False,
            },
            "inputs": {
                **{name: {"sha256": value} for name, value in sources.items()},
                "outcomes": {"sha256": "f" * 64},
            },
            "market_mapping": [{**row, "winner_up": 1} for row in universe],
            "source_coverage": {
                "target_trade_involvement_rows": 62,
                "accepted_fee_zero_v2_maker_sale_rows": 62,
                "erc1155_coverage_status": (
                    "known_incomplete_token_mapping_not_custody_complete"
                ),
                "erc1155_custody_complete": False,
                "erc1155_mapped_token_rows": 0,
                "source_watermark_unix_s": {
                    "erc1155_transfers": 96_000,
                    "redemptions": 96_000,
                    "trade_history": 96_000,
                },
            },
            "counts": {"markets": 31, "maker_sells": 62},
            "windows": windows,
            "capital": {
                "portfolio_peak_mechanics_implied_collateral_requirement_base": "100"
            },
            "totals": {
                "contractual_terminal_pnl_base": str(terminal_total),
                "contractual_pair_recovery_residual_zero_floor_pnl_base": str(
                    terminal_total
                ),
                "split_principal_base": "3100",
                "sale_cash_base": str(
                    sum(int(str(row["sale_cash_base"])) for row in windows)
                ),
                "merge_return_base": "0",
                "rebate_overlay": {"endpoint_base": None},
            },
        }
        ledgers.append((wallet, ledger, f"{wallet_index + 1:064x}"))
    return cohort, ledgers


def test_finite_integer_gate_passes_and_preserves_aggregate_economics() -> None:
    cohort, ledgers = _fixture()

    result = aggregate(cohort, ledgers, _revision())
    gate = _object(result["pre_registered_gate"])
    wallet_gate = _object(gate["wallet_gate"])
    window_gate = _object(gate["market_window_gate"])

    assert result["schema"] == "project-fail-mint-sibling-falsification-v2"
    assert result["counts"] == {"wallets": 10, "windows": 31, "wallet_windows": 310}
    assert result["claim_limits"]["erc1155_custody_complete"] is False
    assert result["totals"] == {
        "terminal_pnl_base_ex_rebate": "311",
        "residual_zero_floor_pnl_base_ex_rebate": "311",
        "split_principal_base": "31000",
        "sale_cash_base": "31311",
        "merge_return_base": "0",
        "profitable_wallets_terminal_ex_rebate": 10,
        "profitable_wallet_windows_terminal_ex_rebate": 310,
        "both_outcomes_sold_wallet_windows": 310,
    }
    assert wallet_gate == {
        "positive_wallets": 10,
        "required_positive_wallets": 7,
        "strictly_positive": True,
        "pass": True,
    }
    assert window_gate["sum_x_base"] == "311"
    assert window_gate["sum_x2_base2"] == "3121"
    assert window_gate[
        "variance_numerator_n_sum_x2_minus_sum_x_squared_base2"
    ] == "30"
    assert window_gate["comparison_lhs"] == "2901630"
    assert window_gate["comparison_rhs"] == "120"
    assert gate["verdict"] == "survives_pre_registered_gates"
    assert gate["authorization"] == "one_prospective_causal_paper_design_only"
    assert gate["incomplete_ledger_policy"] == (
        "inconclusive_no_result_never_drop_or_replace_wallets"
    )
    wallets = result["wallets"]
    assert isinstance(wallets, list)
    assert _object(wallets[0])["capital"] == {
        "portfolio_peak_mechanics_implied_collateral_requirement_base": "100"
    }


def test_constant_positive_windows_have_zero_variance_and_do_not_pass() -> None:
    cohort, ledgers = _fixture(lambda _wallet, _window: 1)

    gate = _object(aggregate(cohort, ledgers, _revision())["pre_registered_gate"])
    wallet_gate = _object(gate["wallet_gate"])
    window_gate = _object(gate["market_window_gate"])

    assert wallet_gate["pass"] is True
    assert window_gate["mean_positive"] is True
    assert window_gate["finite_positive_variance"] is False
    assert window_gate["t_at_least_minimum_exact"] is False
    assert window_gate[
        "variance_numerator_n_sum_x2_minus_sum_x_squared_base2"
    ] == "0"
    assert gate["pass"] is False
    assert gate["verdict"] == "fails_pre_registered_gates"
    assert gate["authorization"] == "none"


def test_six_positive_and_four_zero_wallets_fail_strict_wallet_threshold() -> None:
    cohort, ledgers = _fixture(
        lambda wallet, window: (2 if wallet < 6 else 0) + int(wallet == window == 0)
    )

    gate = _object(aggregate(cohort, ledgers, _revision())["pre_registered_gate"])
    wallet_gate = _object(gate["wallet_gate"])
    window_gate = _object(gate["market_window_gate"])

    assert wallet_gate["positive_wallets"] == 6
    assert wallet_gate["pass"] is False
    assert window_gate["t_at_least_minimum_exact"] is True
    assert gate["verdict"] == "fails_pre_registered_gates"


def test_missing_wallet_aborts_without_a_partial_result() -> None:
    cohort, ledgers = _fixture()

    with pytest.raises(EvidenceError, match="exact frozen wallet cohort"):
        aggregate(cohort, ledgers[:-1], _revision())


def test_gate_spec_drift_and_rebate_artifacts_fail_closed() -> None:
    cohort, ledgers = _fixture()
    drifted = deepcopy(cohort)
    _object(drifted["gate_spec"])["required_positive_wallets"] = 6
    with pytest.raises(EvidenceError, match="gate spec differs"):
        aggregate(drifted, ledgers, _revision())

    rebated = deepcopy(ledgers)
    _object(rebated[0][1]["inputs"])["rebate"] = {"sha256": "9" * 64}
    with pytest.raises(EvidenceError, match="rebate artifacts are forbidden"):
        aggregate(cohort, rebated, _revision())

    endpoint_only = deepcopy(ledgers)
    totals = _object(endpoint_only[0][1]["totals"])
    _object(totals["rebate_overlay"])["endpoint_base"] = "1"
    with pytest.raises(EvidenceError, match="rebate endpoints are forbidden"):
        aggregate(cohort, endpoint_only, _revision())


def test_wrong_ledger_lifecycle_or_incomplete_source_watermark_fails_closed() -> None:
    cohort, ledgers = _fixture()
    stale_candidate = deepcopy(cohort)
    lifecycle = _object(stale_candidate["accounting_lifecycle"])
    _object(lifecycle["source_watermark_unix_s"])["splits_merges"] = 95_999
    with pytest.raises(EvidenceError, match="candidate sources end before"):
        aggregate(stale_candidate, ledgers, _revision())

    wrong_scope = deepcopy(ledgers)
    _object(wrong_scope[0][1]["scope"])["lifecycle_end_exclusive"] = 95_999
    with pytest.raises(EvidenceError, match="ledger scope does not match"):
        aggregate(cohort, wrong_scope, _revision())

    stale_source = deepcopy(ledgers)
    coverage = _object(stale_source[0][1]["source_coverage"])
    _object(coverage["source_watermark_unix_s"])["trade_history"] = 95_999
    with pytest.raises(EvidenceError, match="sources end before"):
        aggregate(cohort, stale_source, _revision())
