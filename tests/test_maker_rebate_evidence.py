from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from tools.adapter_receipt_input import CANDIDATE_SCHEMA
from tools.maker_rebate_evidence import (
    RebateEvidenceError,
    _canonical,
    _sha256,
    _write_exclusive,
    evidence_payload,
    join_response,
    load_market_artifact,
    query_url,
)

DAY = "2026-08-25"
WALLET = "0x" + "ab" * 20
API_WALLET = "0x" + "Ab" * 20
ASSET = "0x" + "34" * 20
CONDITIONS = ("0x" + "ab" * 32, "0x" + "cd" * 32, "0x" + "ef" * 32)


def _artifact() -> dict[str, object]:
    mapping = [
        {
            "asset": "btc", "condition_id": CONDITIONS[0], "down_token": "102",
            "slug": "btc-updown-5m-1000", "start": 1000,
            "up_token": "101", "winner_up": 1,
        },
        {
            "asset": "btc", "condition_id": CONDITIONS[1], "down_token": "202",
            "slug": "btc-updown-5m-1300", "start": 1300,
            "up_token": "201", "winner_up": 0,
        },
    ]
    candidate_query = {"schema": "bounded-test-query-v1", "sql": "SELECT bounded"}
    return {
        "schema": CANDIDATE_SCHEMA,
        "query": {
            "candidate_query_sha256": _sha256(_canonical(candidate_query)),
            "chain_id": 137, "cohort_sha256": "00" * 32,
            "end_block": 101, "lifecycle_end_exclusive": 1001,
            "lifecycle_start": 1000,
            "market_mapping_sha256": _sha256(_canonical(mapping)), "start_block": 100,
        },
        "candidate_query": candidate_query, "market_mapping": mapping,
        "counts": {"candidates": 1},
        "candidates": [{
            "source_block_number": 100, "source_log_index": 1,
            "source_block_timestamp": 1000, "tx_hash": "0x" + "56" * 32,
            "condition_id": CONDITIONS[0], "op": "split",
            "adapter": "0x" + "78" * 20, "amount": "1",
            "token_ids": ["101", "102"],
        }],
    }


def _row(condition: str, amount: str = "0.237519") -> dict[str, str]:
    return {
        "date": f"{DAY}T00:00:00Z", "condition_id": condition, "asset_address": ASSET,
        "maker_address": API_WALLET, "rebated_fees_usdc": amount,
    }


def test_exact_market_join_total_and_exclusive_evidence(tmp_path: Path) -> None:
    source = tmp_path / "candidates.json"
    source.write_text(json.dumps(_artifact()), encoding="utf-8")
    artifact = load_market_artifact(source)
    response = _canonical([
        _row(CONDITIONS[1], "1.000001"),
        _row(CONDITIONS[2], "9.5"),
        _row(CONDITIONS[0]),
    ])
    url = query_url(DAY, WALLET)
    payload = evidence_payload(
        response, DAY, WALLET, artifact, url,
        {"git_head": "0" * 40, "revision_sha256": "1" * 64}, source,
    )

    assert payload["total_rebated_fees_usdc"] == "1.23752"
    assert payload["counts"] == {
        "response_rows": 3, "selected_rows": 2,
        "unmapped_response_conditions": 1,
        "mapped_conditions_without_response": 0,
    }
    selected = cast(list[dict[str, object]], payload["selected_rows"])
    assert [row["condition_id"] for row in selected] == sorted(CONDITIONS[:2])
    assert payload["unmapped_response_condition_ids"] == [CONDITIONS[2]]
    assert payload["raw_response_sha256"] == _sha256(response)
    assert url == f"https://clob.polymarket.com/rebates/current?date={DAY}&maker_address={WALLET}"

    output = tmp_path / "rebates.json"
    digest = _write_exclusive(output, payload)
    assert digest == _sha256(output.read_bytes())
    with pytest.raises(FileExistsError):
        _write_exclusive(output, payload)


def test_missing_mapping_and_malformed_duplicate_or_incomplete_response_fail_closed(
    tmp_path: Path,
) -> None:
    invalid_artifact = _artifact()
    del invalid_artifact["market_mapping"]
    source = tmp_path / "invalid-candidates.json"
    source.write_text(json.dumps(invalid_artifact), encoding="utf-8")
    with pytest.raises(RebateEvidenceError, match="invalid candidate artifact"):
        load_market_artifact(source)

    valid_source = tmp_path / "valid-candidates.json"
    valid_source.write_text(json.dumps(_artifact()), encoding="utf-8")
    artifact = load_market_artifact(valid_source)
    incomplete = _row(CONDITIONS[0])
    del incomplete["rebated_fees_usdc"]
    wrong_maker = _row(CONDITIONS[0])
    wrong_maker["maker_address"] = "0x" + "99" * 20
    wrong_day = _row(CONDITIONS[0])
    wrong_day["date"] = "2026-08-24T00:00:00Z"
    non_midnight = _row(CONDITIONS[0])
    non_midnight["date"] = f"{DAY}T00:00:01Z"
    trailing_date_data = _row(CONDITIONS[0])
    trailing_date_data["date"] = f"{DAY}T00:00:00Z-extra"
    cases = (
        b"{}",
        _canonical([_row(CONDITIONS[0]), _row(CONDITIONS[0], "0.1")]),
        _canonical([incomplete]),
        _canonical([wrong_maker]),
        _canonical([wrong_day]),
        _canonical([non_midnight]),
        _canonical([trailing_date_data]),
    )
    for response in cases:
        with pytest.raises(RebateEvidenceError):
            join_response(response, DAY, WALLET, artifact)
