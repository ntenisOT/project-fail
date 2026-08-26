from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Mapping

from eth_utils import keccak
import pytest

import tools.mint_accounting_outcomes as outcome_module
from tools.market_windows import ResolvedWindow
from tools.mint_accounting import _finality_gate, _window_accounting
from tools.mint_accounting_capital import capital_diagnostics
from tools.mint_accounting_clickhouse import _fill_sql, fill_events, redemption_events
from tools.mint_accounting_inputs import (
    EvidenceError,
    SOURCE_PATHS,
    canonical,
    sha256,
    verify_receipts,
)
from tools.mint_accounting_outcomes import _validate_window, verified_winners


def _window(start: int, suffix: int, winner_up: int = 0) -> ResolvedWindow:
    return ResolvedWindow(
        slug=f"btc-updown-5m-{start}", asset="btc", start=start,
        condition_id="0x" + f"{suffix:064x}", up_token=str(1000 + suffix),
        down_token=str(2000 + suffix), winner_up=winner_up,
    )


def _producer_revision() -> dict[str, object]:
    frozen = {
        "git_head": "a" * 40,
        "source_sha256": {name: "b" * 64 for name in SOURCE_PATHS},
        "runtime": {
            "python_implementation": "cpython",
            "python_version": "3.14.2",
            "clickhouse_connect_version": "0.15.1",
            "eth_utils_version": "6.0.0",
        },
    }
    return {**frozen, "revision_sha256": sha256(canonical(frozen))}


def _split(window: ResolvedWindow, amount: int, block: int, timestamp: int) -> dict[str, object]:
    return {
        "type": "split", "block_number": block, "timestamp": timestamp, "log_index": 1,
        "tx_hash": "0x" + f"{block:064x}", "condition_id": window.condition_id,
        "adapter": "0x" + "11" * 20, "amount_base": str(amount),
        "token_ids": [window.up_token, window.down_token],
    }


def _sale(
    window: ResolvedWindow, side: str, shares: int, cash: int, block: int, log: int,
) -> dict[str, object]:
    return {
        "type": "maker_sell", "block_number": block, "timestamp": window.start + 10,
        "log_index": log, "tx_hash": "0x" + f"{block * 10 + log:064x}",
        "condition_id": window.condition_id, "side": side, "shares_base": str(shares),
        "cash_delta_base": str(cash),
    }


def test_zero_fill_window_is_explicit_and_merge_ordering_fails_closed() -> None:
    window = _window(1_000, 1)
    split = _split(window, 100, 10, 900)
    merge = {
        **split, "type": "merge", "block_number": 20, "timestamp": 1_400,
        "log_index": 2, "tx_hash": "0x" + "22" * 32,
    }

    summary, events = _window_accounting(window, [split, merge], [], None)

    assert summary["maker_sell_count"] == 0
    assert summary["contractual_terminal_pnl_base"] == "0"
    assert summary["contractual_pair_recovery_residual_zero_floor_pnl_base"] == "0"
    assert [event["type"] for event in events] == ["split", "merge"]
    with pytest.raises(EvidenceError, match="after the last owner fill"):
        _window_accounting(
            window, [split, {**merge, "block_number": 10, "log_index": 0}], [], None
        )
    with pytest.raises(EvidenceError, match="symmetric observed remainder"):
        _window_accounting(window, [split, {**merge, "amount_base": "99"}], [], None)


def test_partial_two_sided_sales_then_nonzero_merge_reconcile_exactly() -> None:
    window = _window(1_000, 1)
    split = _split(window, 100, 10, 900)
    fills = [
        _sale(window, "up", 40, 30, 20, 1),
        _sale(window, "down", 40, 30, 20, 2),
    ]
    merge = {
        **split,
        "type": "merge",
        "amount_base": "60",
        "block_number": 30,
        "timestamp": 1_200,
        "log_index": 3,
        "tx_hash": "0x" + "33" * 32,
    }

    summary, events = _window_accounting(window, [split, merge], fills, None)

    assert summary["maker_sell_count"] == 2
    assert summary["merge_base"] == "60"
    assert summary["contractual_terminal_pnl_base"] == "20"
    assert [event["type"] for event in events] == [
        "split", "maker_sell", "maker_sell", "merge",
    ]


def test_capital_reports_portfolio_and_non_cross_netted_paths_separately() -> None:
    first, second = _window(1_000, 1), _window(1_300, 2)
    first_events = [
        _split(first, 100, 10, 900),
        _sale(first, "up", 100, 75, 20, 1),
        _sale(first, "down", 100, 75, 20, 2),
    ]
    second_events = [_split(second, 150, 30, 1_250)]
    first_summary, first_audit = _window_accounting(first, first_events[:1], first_events[1:], None)
    second_summary, second_audit = _window_accounting(second, second_events, [], None)

    result = capital_diagnostics(
        [first_summary, second_summary], [*first_audit, *second_audit], 800, 2_000, None
    )

    assert result["portfolio_peak_mechanics_implied_collateral_requirement_base"] == "100"
    assert result[
        "non_cross_netted_peak_mechanics_implied_collateral_requirement_base"
    ] == "150"
    assert result["ledger_collateral_equivalent_at_end_base"] == "-100"
    assert result[
        "non_cross_netted_mechanics_implied_collateral_requirement_at_end_base"
    ] == "150"
    assert result["rebate_recycled_inside_scope_base"] == "0"


def test_fill_normalization_accepts_only_unique_fee_zero_v2_maker_sales() -> None:
    window = _window(1_000, 1)
    wallet = "0x" + "12" * 20
    valid = (
        10, 1_010, 3, "0x" + "ab" * 32, "order", wallet, "counterparty",
        window.up_token, "0", "5", "3", "0", False, "", 0, True, True,
    )

    assert fill_events([valid], [window], wallet)[0]["cash_delta_base"] == "3"
    fee = list(valid)
    fee[11] = "1"
    with pytest.raises(EvidenceError, match="fee-zero V2 maker sale"):
        fill_events([tuple(fee)], [window], wallet)
    buy = list(valid)
    buy[7], buy[8] = "0", window.up_token
    with pytest.raises(EvidenceError, match="fee-zero V2 maker sale"):
        fill_events([tuple(buy)], [window], wallet)
    with pytest.raises(EvidenceError, match="duplicate owner fill"):
        fill_events([valid, valid], [window], wallet)
    self_trade = list(valid)
    self_trade[6] = wallet
    with pytest.raises(EvidenceError, match="self-trades"):
        fill_events([tuple(self_trade)], [window], wallet)
    taker_only = list(valid)
    taker_only[5], taker_only[6], taker_only[15], taker_only[16] = (
        "counterparty", wallet, False, False,
    )
    with pytest.raises(EvidenceError, match="fee-zero V2 maker sale"):
        fill_events([tuple(taker_only)], [window], wallet)
    sql = _fill_sql(900, 2_000, wallet)
    assert f"(lower(maker)='{wallet}' OR lower(taker)='{wallet}')" in sql


def test_any_mapped_redemption_fails_until_consumption_is_accounted() -> None:
    wallet = "0x" + "12" * 20
    condition = "0x" + "34" * 32
    row = (
        10, 1_100, 2, "0x" + "56" * 32, wallet, condition,
        "0x" + "78" * 20, "0x" + "00" * 32, [1, 2], "1",
    )

    with pytest.raises(EvidenceError, match="token-consumption accounting"):
        redemption_events([row], {condition}, wallet)
    zero = list(row)
    zero[-1] = "0"
    with pytest.raises(EvidenceError, match="even at zero payout"):
        redemption_events([tuple(zero)], {condition}, wallet)


def test_receipt_cache_rehashes_embedded_receipt_and_transaction(tmp_path: Path) -> None:
    tx_hash = "0x" + "12" * 32
    receipt = {"transactionHash": tx_hash, "status": "0x1"}
    receipt_sha = sha256(canonical(receipt))
    row = {
        "schema": "project-fail-polygon-receipt-cache-v1",
        "tx_hash": tx_hash,
        "receipt_sha256": receipt_sha,
        "receipt": receipt,
    }
    path = tmp_path / "receipts.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    verify_receipts(path, sha256(path.read_bytes()), {tx_hash: receipt_sha})
    row["receipt"]["status"] = "0x0"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(EvidenceError, match="content or transaction"):
        verify_receipts(path, sha256(path.read_bytes()), {tx_hash: receipt_sha})


def test_finalized_anchor_must_cover_tail_and_every_lifecycle_event() -> None:
    chain = {"block_number": 100, "block_timestamp": 2_000}

    _finality_gate(chain, 1_900, [{"block_number": 100}])
    with pytest.raises(EvidenceError, match="precedes the lifecycle tail"):
        _finality_gate(chain, 2_001, [])
    with pytest.raises(EvidenceError, match="after the finalized payout anchor"):
        _finality_gate(chain, 1_900, [{"block_number": 101}])


def test_authoritative_payout_vector_binds_resolution_and_position_ids() -> None:
    oracle = "0x" + "11" * 20
    question = "0x" + "22" * 32
    condition = "0x" + keccak(
        bytes.fromhex(oracle[2:]) + bytes.fromhex(question[2:]) + (2).to_bytes(32, "big")
    ).hex()
    window = ResolvedWindow(
        slug="btc-updown-5m-1000", asset="btc", start=1_000,
        condition_id=condition, up_token="101", down_token="202", winner_up=0,
    )
    row = {
        "condition_id": condition, "slug": window.slug, "payout_denominator": "1",
        "payout_numerators": ["0", "1"],
        "position_ids_by_outcome_index": ["101", "202"],
        "up_outcome_index": 0, "down_outcome_index": 1,
        "winner_outcome_index": 1, "winner_up": 0,
        "condition_resolution": {
            "block_number": 90, "block_hash": "0x" + "bb" * 32, "log_index": 3,
            "tx_hash": "0x" + "cc" * 32, "oracle": oracle, "question_id": question,
            "outcome_slot_count": 2, "payout_numerators": ["0", "1"],
        },
    }
    artifact = {
        "schema": "project-fail-ctf-payout-evidence-v1",
        "source": {"candidate_sha256": "d" * 64, "market_mapping_sha256": "e" * 64},
        "chain": {"chain_id": 137,
                  "conditional_tokens": "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
                   "position_id_collateral": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
                   "block_number": 100, "block_hash": "0x" + "aa" * 32,
                   "block_timestamp": 1,
                   "finality_tag": "finalized",
                   "state_call_block_reference": "eip-1898-blockHash-requireCanonical",
                   "rpc_endpoint_sha256": "f" * 64},
        "rows": [row],
        "revision": _producer_revision(),
    }
    expected_revision = _producer_revision()

    assert verified_winners(
        artifact, "d" * 64, "e" * 64, [window], expected_revision
    ) == {condition: False}
    tampered = deepcopy(artifact)
    tampered["rows"][0]["position_ids_by_outcome_index"] = ["101", "303"]
    with pytest.raises(EvidenceError, match="position IDs"):
        verified_winners(tampered, "d" * 64, "e" * 64, [window], expected_revision)
    wrong_revision = deepcopy(artifact)
    wrong_revision["revision"]["git_head"] = "c" * 40
    with pytest.raises(EvidenceError, match="manifest hash"):
        verified_winners(
            wrong_revision, "d" * 64, "e" * 64, [window], expected_revision
        )
    with pytest.raises(EvidenceError, match="unambiguous single winner"):
        _validate_window(window, 2, (1, 1), (101, 202))


def test_payout_collection_pins_finalized_hash_and_rechecks_canonical_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = "0x" + "11" * 20
    question = "0x" + "22" * 32
    condition = "0x" + keccak(
        bytes.fromhex(oracle[2:]) + bytes.fromhex(question[2:]) + (2).to_bytes(32, "big")
    ).hex()
    window = ResolvedWindow(
        slug="btc-updown-5m-1000", asset="btc", start=1_000,
        condition_id=condition, up_token="101", down_token="202", winner_up=0,
    )
    block_hash = "0x" + "aa" * 32
    reorg = {"active": False}
    block_references: list[object] = []

    def fake_rpc(_url: str, method: str, params: list[object], *_args: object) -> object:
        if method == "eth_chainId":
            return hex(137)
        assert method == "eth_getBlockByNumber"
        if params[0] == "finalized":
            return {"number": hex(100), "hash": block_hash, "timestamp": hex(1_500)}
        return {
            "number": hex(100),
            "hash": "0x" + ("cc" if reorg["active"] else "aa") * 32,
            "timestamp": hex(1_500),
        }

    def fake_batch(
        _url: str, calls: Mapping[str, str], reference: object, *_args: object,
    ) -> dict[str, str]:
        block_references.append(reference)
        output: dict[str, str] = {}
        for key in calls:
            if ":denominator" in key or ":numerator:1" in key:
                value = 1
            elif ":position:0" in key:
                value = 101
            elif ":position:1" in key:
                value = 202
            else:
                value = 0
            output[key] = "0x" + f"{value:064x}"
        return output

    resolution_log = {
        "address": outcome_module.CTF,
        "removed": False,
        "topics": [
            outcome_module.CONDITION_RESOLUTION_TOPIC,
            condition,
            "0x" + "0" * 24 + oracle[2:],
            question,
        ],
        "data": "0x" + "".join(f"{value:064x}" for value in (2, 64, 2, 0, 1)),
        "blockHash": "0x" + "dd" * 32,
        "transactionHash": "0x" + "ee" * 32,
        "blockNumber": hex(90),
        "logIndex": hex(3),
    }
    monkeypatch.setattr(outcome_module, "_rpc", fake_rpc)
    monkeypatch.setattr(outcome_module, "_batch_calls", fake_batch)
    monkeypatch.setattr(
        outcome_module, "_resolution_logs",
        lambda *_args: {condition: resolution_log},
    )

    artifact = outcome_module.fetch_evidence(
        [window], "d" * 64, "e" * 64, 1, "https://rpc.invalid",
        timeout_s=1.0, attempts=1,
    )
    expected_reference = {"blockHash": block_hash, "requireCanonical": True}
    assert block_references == [expected_reference, expected_reference]
    assert artifact["chain"]["finality_tag"] == "finalized"
    reorg["active"] = True
    with pytest.raises(EvidenceError, match="finalized block changed"):
        outcome_module.fetch_evidence(
            [window], "d" * 64, "e" * 64, 1, "https://rpc.invalid",
            timeout_s=1.0, attempts=1,
        )
