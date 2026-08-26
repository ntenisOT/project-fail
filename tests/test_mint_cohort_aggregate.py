from __future__ import annotations

from copy import deepcopy

import pytest

from tools.mint_accounting_inputs import EvidenceError, SOURCE_PATHS, canonical, sha256
from tools.mint_cohort_aggregate import aggregate


WALLETS = ["0x" + "11" * 20, "0x" + "22" * 20]


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


def _fixture() -> tuple[dict[str, object], list[tuple[str, dict[str, object], str]]]:
    revision = _revision()
    universe = [
        {
            "asset": "btc", "slug": f"btc-updown-5m-{start}", "start": start,
            "condition_id": "0x" + f"{index:064x}",
            "up_token": str(100 + index), "down_token": str(200 + index),
        }
        for index, start in enumerate((300, 600), 1)
    ]
    sources = {"candidate": "c" * 64, "attribution": "d" * 64,
               "receipt_cache": "e" * 64}
    cohort = {
        "schema": "project-fail-mint-sibling-cohort-proof-v1",
        "revision": revision,
        "wallets": WALLETS,
        "outcome_free_universe": universe,
        "counts": {"wallets": 2, "windows": 2, "wallet_windows": 4},
        "source_sha256": sources,
    }
    ledgers = []
    for wallet_index, wallet in enumerate(WALLETS):
        windows = []
        for window_index, market in enumerate(universe):
            terminal = 5 if wallet_index == window_index else -2
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
        terminal_total = sum(int(row["contractual_terminal_pnl_base"]) for row in windows)
        ledger = {
            "schema": "project-fail-mint-observed-accounting-v2",
            "revision": revision,
            "scope": {"wallet": wallet, "complete_wallet": False, "cash_realized": False},
            "inputs": {
                **{name: {"sha256": value} for name, value in sources.items()},
                "outcomes": {"sha256": "f" * 64},
            },
            "market_mapping": [{**row, "winner_up": 1} for row in universe],
            "source_coverage": {"target_trade_involvement_rows": 4,
                                "accepted_fee_zero_v2_maker_sale_rows": 4},
            "counts": {"markets": 2, "maker_sells": 4},
            "windows": windows,
            "capital": {
                "portfolio_peak_mechanics_implied_collateral_requirement_base": "100"
            },
            "totals": {
                "contractual_terminal_pnl_base": str(terminal_total),
                "contractual_pair_recovery_residual_zero_floor_pnl_base": str(
                    terminal_total
                ),
                "split_principal_base": "200",
                "sale_cash_base": str(sum(int(row["sale_cash_base"]) for row in windows)),
                "merge_return_base": "0",
                "rebate_overlay": {"endpoint_base": None},
            },
        }
        ledgers.append((wallet, ledger, str(wallet_index + 1) * 64))
    return cohort, ledgers


def test_fixed_grid_aggregate_preserves_per_wallet_capital_and_exact_economics() -> None:
    cohort, ledgers = _fixture()

    result = aggregate(cohort, ledgers, _revision())

    assert result["counts"] == {"wallets": 2, "windows": 2, "wallet_windows": 4}
    assert result["totals"]["terminal_pnl_base_ex_rebate"] == "6"
    assert result["totals"]["profitable_wallet_windows_terminal_ex_rebate"] == 2
    assert result["totals"]["both_outcomes_sold_wallet_windows"] == 4
    assert result["capital_policy"].startswith("per-wallet paths only")
    assert result["wallets"][0]["capital"] == {
        "portfolio_peak_mechanics_implied_collateral_requirement_base": "100"
    }


def test_missing_wallet_or_mixed_rebate_evidence_aborts_the_whole_aggregate() -> None:
    cohort, ledgers = _fixture()

    with pytest.raises(EvidenceError, match="exact frozen wallet cohort"):
        aggregate(cohort, ledgers[:1], _revision())
    mixed = deepcopy(ledgers)
    second = mixed[1][1]
    second["inputs"]["rebate"] = {"sha256": "9" * 64}
    second["totals"]["rebate_overlay"]["endpoint_base"] = "2"
    for row in second["windows"]:
        row["rebate_endpoint_base"] = "1"
    with pytest.raises(EvidenceError, match="mix absent and complete rebate"):
        aggregate(cohort, mixed, _revision())
