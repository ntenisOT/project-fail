from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tools.evidence_provenance import (
    ATTRIBUTION_PRODUCER_PATHS,
    CANDIDATE_PRODUCER_PATHS,
)
from tools.mint_accounting_inputs import EvidenceError, SOURCE_PATHS, canonical, sha256
from tools.mint_falsification_gate import GATE_SPEC
from tools.mint_sibling_cohort import derive, verify


ADAPTER = "0xada100db00ca00073811820692005400218fce1f"
REFERENCE = "0x" + "11" * 20
SIBLINGS = tuple("0x" + f"{index:040x}" for index in range(1, 11))
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
    starts = tuple(range(300, 9_301, 300))
    market_rows = [
        {
            "asset": "btc", "slug": f"btc-updown-5m-{start}", "start": start,
            "condition_id": "0x" + f"{index:064x}",
            "up_token": str(100 + index), "down_token": str(200 + index),
            "winner_up": index % 2,
        }
        for index, start in enumerate(starts, 1)
    ]
    candidate_rows, result_rows = [], []
    counter = 1
    for wallet in (REFERENCE, *SIBLINGS):
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
                "receipt_sha256": sha256(canonical({
                    "transactionHash": row["tx_hash"], "status": "0x1",
                })),
            })
            counter += 1
    accounting_end = starts[-1] + 300 + 86_400
    candidate_query: dict[str, object] = {
        "lifecycle_start": 100,
        "lifecycle_end_exclusive": accounting_end,
        "post_close_tail_s": 86_400,
    }
    query = {
        "chain_id": 137, "market_mapping_sha256": sha256(canonical(market_rows)),
        "candidate_query_sha256": sha256(canonical(candidate_query)),
        "start": starts[0], "end": starts[-1],
        "lifecycle_start": 100, "lifecycle_end_exclusive": accounting_end,
        "source_watermark_unix_s": {
            "splits_merges": accounting_end, "trade_history": accounting_end,
        },
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
        "settings": {
            "amount_tolerance_base_units": 0, "max_candidates": len(candidate_rows),
        },
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
            "wallet": str(result["attribution"]["wallet"]),
            "condition_id": row["condition_id"],
            "tx_hash": row["tx_hash"], "source_block_number": row["source_block_number"],
            "source_log_index": row["source_log_index"], "amount": row["amount"],
            "token_ids": row["token_ids"], "receipt_sha256": result["receipt_sha256"],
        }
        for row, result in zip(
            candidate_rows[len(market_rows):],
            result_rows[len(market_rows):],
            strict=True,
        )
    ]
    split_rows.sort(key=lambda row: (
        row["wallet"], row["condition_id"], row["source_block_number"], row["source_log_index"],
    ))
    manifest = {
        "schema": "project-fail-mint-sibling-cohort-v1",
        "sources": {
            name: {"path": f"out/historical-{name}.missing", "sha256": "d" * 64}
            for name in ("candidate", "attribution", "receipt_cache")
        },
        "universe": {
            "chain_id": 137, "asset": "btc", "start": starts[0], "end": starts[-1],
            "window_count": len(starts),
            "market_mapping_sha256": sha256(canonical(market_rows)),
            "outcome_free_projection_sha256": sha256(canonical(universe_rows)),
        },
        "selection": {
            "adapter": ADAPTER, "adapter_kind": "standard",
            "candidate_scope": "current_adapter_lifecycle", "op": "split",
            "amount_base": "750000000", "classification": "explicit_wallet",
            "required_conditions": len(starts), "reference_wallet": REFERENCE,
            "qualifying_precursor_wallet_count": len(SIBLINGS) + 1,
            "qualifying_precursor_wallets_sha256": sha256(
                canonical(sorted([REFERENCE, *SIBLINGS]))
            ),
        },
        "selection_lifecycle": {
            "lifecycle_start": 100,
            "lifecycle_end_exclusive": starts[-1] + 300 + 3_300,
            "post_last_market_close_s": 3_300,
            "purpose": "historical_membership_reproduction_only",
        },
        "wallets": list(SIBLINGS),
        "expected_grid": {"wallets": 10, "windows_per_wallet": 31,
                          "wallet_windows": 310, "qualifying_splits": 310},
        "projections": {"wallets_sha256": sha256(canonical(list(SIBLINGS))),
                        "split_evidence_sha256": sha256(canonical(split_rows))},
        "pre_registered_gate": dict(GATE_SPEC),
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

    assert proof["wallets"] == changed_proof["wallets"] == list(SIBLINGS)
    later_candidate = deepcopy(candidate)
    later_attribution = deepcopy(attribution)
    later = deepcopy(later_candidate["candidates"][31])
    later.update({
        "tx_hash": "0x" + "ee" * 32,
        "source_block_number": 998,
        "source_log_index": 998,
        "source_block_timestamp": manifest["selection_lifecycle"][
            "lifecycle_end_exclusive"
        ],
    })
    later_candidate["candidates"].append(later)
    later_candidate["counts"]["candidates"] += 1
    later_attribution["results"].append({
        "candidate": {name: deepcopy(later[name]) for name in CORE_FIELDS},
        "attribution": {
            "classification": "explicit_wallet", "wallet": SIBLINGS[0],
            "counterparty": SIBLINGS[0],
        },
        "receipt_sha256": "e" * 64,
    })
    later_attribution["counts"]["candidates"] += 1
    later_attribution["counts"]["transactions"] += 1
    later_attribution["counts"]["explicit_wallet"] += 1
    later_attribution["settings"]["max_candidates"] += 1
    later_proof, _ = derive(
        later_candidate, later_attribution, manifest, CANDIDATE_SHA, _revision()
    )
    assert later_proof["wallets"] == list(SIBLINGS)
    truncated = deepcopy(attribution)
    truncated["results"] = truncated["results"][:-1]
    with pytest.raises(EvidenceError, match="omit or duplicate"):
        derive(candidate, truncated, manifest, CANDIDATE_SHA, _revision())
    duplicated_candidate = deepcopy(candidate)
    duplicated_attribution = deepcopy(attribution)
    duplicate = deepcopy(duplicated_candidate["candidates"][len(proof["outcome_free_universe"])])
    duplicate.update({"tx_hash": "0x" + "ff" * 32, "source_block_number": 99,
                      "source_log_index": 99})
    duplicated_candidate["candidates"].append(duplicate)
    duplicated_attribution["results"].append({
        "candidate": {name: deepcopy(duplicate[name]) for name in CORE_FIELDS},
        "attribution": {"classification": "explicit_wallet", "wallet": SIBLINGS[0],
                        "counterparty": SIBLINGS[0]},
        "receipt_sha256": "f" * 64,
    })
    duplicated_candidate["counts"]["candidates"] += 1
    duplicated_attribution["counts"]["candidates"] += 1
    duplicated_attribution["counts"]["transactions"] += 1
    duplicated_attribution["counts"]["explicit_wallet"] += 1
    duplicated_attribution["settings"]["max_candidates"] += 1
    with pytest.raises(EvidenceError, match="precursor cohort changed"):
        derive(
            duplicated_candidate, duplicated_attribution, manifest,
            CANDIDATE_SHA, _revision(),
        )


def test_unresolved_global_attribution_fails_even_for_reference_wallet() -> None:
    candidate, attribution, manifest = _fixture()
    attribution["results"][0]["attribution"]["classification"] = "unresolved"
    attribution["counts"]["explicit_wallet"] -= 1
    attribution["counts"]["unresolved"] += 1

    with pytest.raises(EvidenceError, match="unresolved candidate"):
        derive(candidate, attribution, manifest, CANDIDATE_SHA, _revision())


def test_current_sources_are_explicit_and_historical_selection_files_are_not_loaded(
    tmp_path: Path,
) -> None:
    candidate, attribution, manifest = _fixture()
    receipt_rows = []
    for result in attribution["results"]:
        tx_hash = result["candidate"]["tx_hash"]
        receipt = {"transactionHash": tx_hash, "status": "0x1"}
        receipt_sha = sha256(canonical(receipt))
        assert result["receipt_sha256"] == receipt_sha
        receipt_rows.append({
            "schema": "project-fail-polygon-receipt-cache-v1",
            "tx_hash": tx_hash,
            "receipt_sha256": receipt_sha,
            "receipt": receipt,
        })

    out = tmp_path / "out"
    out.mkdir()
    candidate_path = out / "current-candidate.json"
    candidate_raw = canonical(candidate)
    candidate_path.write_bytes(candidate_raw)
    candidate_sha = sha256(candidate_raw)
    attribution["input_sha256"] = candidate_sha
    attribution_path = out / "current-attribution.json"
    attribution_raw = canonical(attribution)
    attribution_path.write_bytes(attribution_raw)
    attribution_sha = sha256(attribution_raw)
    receipt_path = out / "current-receipts.jsonl"
    receipt_raw = b"".join(canonical(row) + b"\n" for row in receipt_rows)
    receipt_path.write_bytes(receipt_raw)
    receipt_sha = sha256(receipt_raw)

    manifest_path = tmp_path / "research" / "mint_sibling_cohort_v1.json"
    manifest_path.parent.mkdir()
    manifest_raw = canonical(manifest)
    manifest_path.write_bytes(manifest_raw)
    expected_revision = deepcopy(_revision())
    expected_revision["source_sha256"]["research/mint_sibling_cohort_v1.json"] = sha256(
        manifest_raw
    )
    frozen_revision = {
        key: expected_revision[key] for key in ("git_head", "source_sha256", "runtime")
    }
    expected_revision["revision_sha256"] = sha256(canonical(frozen_revision))

    result = verify(
        tmp_path,
        Path("out/current-candidate.json"), candidate_sha,
        Path("out/current-attribution.json"), attribution_sha,
        Path("out/current-receipts.jsonl"), receipt_sha,
        expected_revision,
    )

    assert result["selection_source_sha256"] == {
        name: "d" * 64 for name in ("attribution", "candidate", "receipt_cache")
    }
    assert result["source_paths"]["candidate"] == "out/current-candidate.json"
    assert not (out / "historical-candidate.missing").exists()

    universe_manifest = manifest["universe"]
    assert isinstance(universe_manifest, dict)
    missing_reference_rows = receipt_rows[int(universe_manifest["window_count"]):]
    incomplete_raw = b"".join(canonical(row) + b"\n" for row in missing_reference_rows)
    receipt_path.write_bytes(incomplete_raw)
    with pytest.raises(EvidenceError, match="receipt hashes do not match"):
        verify(
            tmp_path,
            Path("out/current-candidate.json"), candidate_sha,
            Path("out/current-attribution.json"), attribution_sha,
            Path("out/current-receipts.jsonl"), sha256(incomplete_raw),
            expected_revision,
        )

    receipt_path.write_bytes(receipt_raw)
    manifest_path.write_bytes(manifest_raw + b" ")
    with pytest.raises(EvidenceError, match="SHA-256 mismatch"):
        verify(
            tmp_path,
            Path("out/current-candidate.json"), candidate_sha,
            Path("out/current-attribution.json"), attribution_sha,
            Path("out/current-receipts.jsonl"), receipt_sha,
            expected_revision,
        )
