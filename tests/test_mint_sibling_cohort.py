from __future__ import annotations

from copy import deepcopy

import pytest

from tools.evidence_provenance import (
    ATTRIBUTION_PRODUCER_PATHS,
    CANDIDATE_PRODUCER_PATHS,
)
from tools.mint_accounting_inputs import EvidenceError, SOURCE_PATHS, canonical, sha256
from tools.mint_sibling_cohort import derive


ADAPTER = "0xada100db00ca00073811820692005400218fce1f"
REFERENCE = "0x" + "11" * 20
SIBLING = "0x" + "22" * 20
CANDIDATE_SHA = "a" * 64
CORE_FIELDS = (
    "source_block_number", "source_log_index", "source_block_timestamp", "tx_hash",
    "condition_id", "op", "adapter", "amount", "token_ids",
)


def _revision() -> dict[str, object]:
    frozen = {
        "git_head": "1" * 40,
        "source_sha256": {name: "b" * 64 for name in SOURCE_PATHS},
        "runtime": {
            "python_implementation": "cpython", "python_version": "3.14.2",
            "clickhouse_connect_version": "0.15.1", "eth_utils_version": "6.0.0",
        },
    }
    return {**frozen, "revision_sha256": sha256(canonical(frozen))}


def _producer(paths: tuple[str, ...], runtime_fields: tuple[str, ...]) -> dict[str, object]:
    expected = _revision()
    runtime = expected["runtime"]
    sources = expected["source_sha256"]
    frozen = {
        "git_head": expected["git_head"],
        "source_sha256": {name: sources[name] for name in paths},
        "runtime": {name: runtime[name] for name in runtime_fields},
    }
    return {**frozen, "revision_sha256": sha256(canonical(frozen))}


def _fixture() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    market_rows = [
        {
            "asset": "btc", "slug": f"btc-updown-5m-{start}", "start": start,
            "condition_id": "0x" + f"{index:064x}",
            "up_token": str(100 + index), "down_token": str(200 + index),
            "winner_up": index % 2,
        }
        for index, start in enumerate((300, 600), 1)
    ]
    candidate_rows, result_rows = [], []
    counter = 1
    for wallet in (REFERENCE, SIBLING):
        for market in market_rows:
            row = {
                "adapter": ADAPTER, "adapter_kind": "standard",
                "candidate_scope": "current_adapter_lifecycle", "amount": "750000000",
                "same_tx_clob": False,
                "condition_id": market["condition_id"], "op": "split",
                "source_block_number": counter, "source_block_timestamp": 200 + counter,
                "source_log_index": counter, "token_ids": [
                    market["up_token"], market["down_token"],
                ],
                "tx_hash": "0x" + f"{counter:064x}",
            }
            candidate_rows.append(row)
            result_rows.append({
                "candidate": {name: deepcopy(row[name]) for name in CORE_FIELDS},
                "attribution": {"classification": "explicit_wallet", "wallet": wallet,
                                "counterparty": wallet},
                "receipt_sha256": f"{counter:064x}",
            })
            counter += 1
    candidate_query: dict[str, object] = {}
    query = {
        "chain_id": 137, "market_mapping_sha256": sha256(canonical(market_rows)),
        "candidate_query_sha256": sha256(canonical(candidate_query)),
    }
    candidate = {
        "schema": "project-fail-adapter-receipt-candidates-v1",
        "query": query,
        "candidate_query": candidate_query,
        "market_mapping": market_rows, "candidates": candidate_rows,
        "counts": {"candidates": len(candidate_rows)},
        "generator": _producer(
            CANDIDATE_PRODUCER_PATHS,
            ("python_implementation", "python_version", "clickhouse_connect_version"),
        ),
    }
    attribution = {
        "schema": "project-fail-adapter-receipt-attribution-v1",
        "input_sha256": CANDIDATE_SHA,
        "query": deepcopy(query), "query_sha256": sha256(canonical(query)),
        "rpc_endpoint_sha256": "c" * 64,
        "settings": {"amount_tolerance_base_units": 0, "max_candidates": 10},
        "revision": _producer(
            ATTRIBUTION_PRODUCER_PATHS, ("python_implementation", "python_version"),
        ),
        "counts": {"candidates": len(result_rows), "transactions": len(result_rows),
                   "clob_atomic": 0, "explicit_wallet": len(result_rows), "unresolved": 0},
        "results": result_rows,
    }
    universe_rows = [
        {key: row[key] for key in (
            "asset", "slug", "start", "condition_id", "up_token", "down_token",
        )}
        for row in market_rows
    ]
    split_rows = [
        {
            "wallet": SIBLING, "condition_id": row["condition_id"],
            "tx_hash": row["tx_hash"], "source_block_number": row["source_block_number"],
            "source_log_index": row["source_log_index"], "amount": row["amount"],
            "token_ids": row["token_ids"], "receipt_sha256": result["receipt_sha256"],
        }
        for row, result in zip(candidate_rows[2:], result_rows[2:], strict=True)
    ]
    split_rows.sort(key=lambda row: (
        row["wallet"], row["condition_id"], row["source_block_number"], row["source_log_index"],
    ))
    manifest = {
        "universe": {
            "chain_id": 137, "asset": "btc", "start": 300, "end": 600,
            "window_count": 2,
            "market_mapping_sha256": sha256(canonical(market_rows)),
            "outcome_free_projection_sha256": sha256(canonical(universe_rows)),
        },
        "selection": {
            "adapter": ADAPTER, "adapter_kind": "standard",
            "candidate_scope": "current_adapter_lifecycle", "op": "split",
            "amount_base": "750000000", "classification": "explicit_wallet",
            "required_conditions": 2, "reference_wallet": REFERENCE,
            "qualifying_precursor_wallet_count": 2,
            "qualifying_precursor_wallets_sha256": sha256(
                canonical(sorted([REFERENCE, SIBLING]))
            ),
        },
        "wallets": [SIBLING],
        "expected_grid": {"wallets": 1, "windows_per_wallet": 2,
                          "wallet_windows": 2, "qualifying_splits": 2},
        "projections": {"wallets_sha256": sha256(canonical([SIBLING])),
                        "split_evidence_sha256": sha256(canonical(split_rows))},
    }
    return candidate, attribution, manifest


def test_membership_is_outcome_independent_and_duplicate_split_fails() -> None:
    candidate, attribution, manifest = _fixture()
    proof, _ = derive(candidate, attribution, manifest, CANDIDATE_SHA, _revision())

    changed_outcomes = deepcopy(candidate)
    for row in changed_outcomes["market_mapping"]:
        row["winner_up"] = 1 - row["winner_up"]
    changed_manifest = deepcopy(manifest)
    changed_mapping_sha = sha256(canonical(changed_outcomes["market_mapping"]))
    changed_outcomes["query"]["market_mapping_sha256"] = changed_mapping_sha
    changed_attribution = deepcopy(attribution)
    changed_attribution["query"] = deepcopy(changed_outcomes["query"])
    changed_attribution["query_sha256"] = sha256(canonical(changed_outcomes["query"]))
    changed_manifest["universe"]["market_mapping_sha256"] = changed_mapping_sha
    changed_proof, _ = derive(
        changed_outcomes, changed_attribution, changed_manifest, CANDIDATE_SHA, _revision()
    )

    assert proof["wallets"] == changed_proof["wallets"] == [SIBLING]
    truncated = deepcopy(attribution)
    truncated["results"] = truncated["results"][:-1]
    with pytest.raises(EvidenceError, match="omit or duplicate"):
        derive(candidate, truncated, manifest, CANDIDATE_SHA, _revision())
    duplicated_candidate = deepcopy(candidate)
    duplicated_attribution = deepcopy(attribution)
    duplicate = deepcopy(duplicated_candidate["candidates"][2])
    duplicate.update({"tx_hash": "0x" + "ff" * 32, "source_block_number": 99,
                      "source_log_index": 99})
    duplicated_candidate["candidates"].append(duplicate)
    duplicated_attribution["results"].append({
        "candidate": {name: deepcopy(duplicate[name]) for name in CORE_FIELDS},
        "attribution": {"classification": "explicit_wallet", "wallet": SIBLING,
                        "counterparty": SIBLING},
        "receipt_sha256": "f" * 64,
    })
    duplicated_candidate["counts"]["candidates"] = 5
    duplicated_attribution["counts"]["candidates"] = 5
    duplicated_attribution["counts"]["transactions"] = 5
    duplicated_attribution["counts"]["explicit_wallet"] = 5
    with pytest.raises(EvidenceError, match="precursor cohort changed"):
        derive(
            duplicated_candidate, duplicated_attribution, manifest,
            CANDIDATE_SHA, _revision(),
        )
