from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tools.adapter_receipt_attributor import ReceiptFetchError, _fetch_missing, _load_cache
from tools.adapter_receipt_core import (
    CTF_ADDRESS,
    DEFAULT_EXCHANGES,
    TRANSFER_BATCH_TOPIC,
    TRANSFER_SINGLE_TOPIC,
    ZERO_ADDRESS,
    Candidate,
    classify_receipt,
)
from tools.adapter_receipt_input import CANDIDATE_SCHEMA, InputError, load_input

ADAPTER = "0x" + "ad" * 20
SAFE = "0x" + "5a" * 20
OPERATOR = "0x" + "01" * 20
TX_HASH = "0x" + "ab" * 32
CONDITION = "0x" + "cd" * 32
TOKENS = (111, 222)
AMOUNT = 1_000_000
BLOCK = 77_000_000
BLOCK_TIMESTAMP = 1_787_690_400


def _word(value: int) -> str:
    return f"{value:064x}"


def _topic_address(address: str) -> str:
    return "0x" + address[2:].rjust(64, "0")


def _batch_data(token_ids: tuple[int, int], values: tuple[int, int]) -> str:
    return "0x" + "".join(
        (
            _word(64),
            _word(160),
            _word(2),
            _word(token_ids[0]),
            _word(token_ids[1]),
            _word(2),
            _word(values[0]),
            _word(values[1]),
        )
    )


def _batch_log(index: int, from_addr: str, to_addr: str, amount: int = AMOUNT) -> dict[str, object]:
    return {
        "address": CTF_ADDRESS,
        "logIndex": hex(index),
        "topics": [
            TRANSFER_BATCH_TOPIC,
            _topic_address(OPERATOR),
            _topic_address(from_addr),
            _topic_address(to_addr),
        ],
        "data": _batch_data(TOKENS, (amount, amount)),
    }


def _single_log(index: int, from_addr: str, to_addr: str, token_id: int) -> dict[str, object]:
    return {
        "address": CTF_ADDRESS,
        "logIndex": hex(index),
        "topics": [
            TRANSFER_SINGLE_TOPIC,
            _topic_address(OPERATOR),
            _topic_address(from_addr),
            _topic_address(to_addr),
        ],
        "data": "0x" + _word(token_id) + _word(AMOUNT),
    }


def _candidate(op: str) -> Candidate:
    return Candidate.from_mapping(
        {
            "source_block_number": BLOCK,
            "source_log_index": 11,
            "source_block_timestamp": BLOCK_TIMESTAMP,
            "tx_hash": TX_HASH,
            "condition_id": CONDITION,
            "op": op,
            "adapter": ADAPTER,
            "amount": str(AMOUNT),
            "token_ids": [str(TOKENS[0]), str(TOKENS[1])],
        }
    )


def _receipt(logs: list[dict[str, object]]) -> dict[str, object]:
    return {"status": "0x1", "blockNumber": hex(BLOCK), "transactionHash": TX_HASH, "logs": logs}


def test_explicit_split_attributes_ordered_adapter_to_safe_batch() -> None:
    logs = [
        _batch_log(10, ZERO_ADDRESS, ADAPTER, AMOUNT + 1),
        _batch_log(12, ADAPTER, SAFE, AMOUNT + 1),
    ]
    result = classify_receipt(
        _candidate("split"),
        _receipt(logs),
        amount_tolerance=1,
    )

    assert result.classification == "explicit_wallet"
    assert result.wallet == SAFE
    assert result.anchor_log_indices == (10,)
    assert result.proof_log_indices == (12,)

    wrong_block = _receipt(logs)
    wrong_block["blockNumber"] = hex(BLOCK + 1)
    mismatch = classify_receipt(_candidate("split"), wrong_block, amount_tolerance=1)
    assert mismatch.classification == "unresolved"
    assert mismatch.reason == "receipt_block_number_mismatch"


def test_explicit_merge_attributes_safe_to_adapter_single_pair_before_burn() -> None:
    result = classify_receipt(
        _candidate("merge"),
        _receipt(
            [
                _single_log(4, SAFE, ADAPTER, TOKENS[0]),
                _single_log(5, SAFE, ADAPTER, TOKENS[1]),
                _batch_log(8, ADAPTER, ZERO_ADDRESS),
            ]
        ),
    )

    assert result.classification == "explicit_wallet"
    assert result.wallet == SAFE
    assert result.proof_log_indices == (4, 5)
    assert result.anchor_log_indices == (8,)


def test_clob_atomic_split_is_exchange_not_safe_and_ambiguity_fails_closed(tmp_path: Path) -> None:
    exchange = sorted(DEFAULT_EXCHANGES)[0]
    base_logs = [
        _batch_log(20, ZERO_ADDRESS, ADAPTER),
        _batch_log(22, ADAPTER, exchange),
        _batch_log(24, ADAPTER, SAFE, AMOUNT + 10),
    ]
    result = classify_receipt(_candidate("split"), _receipt(base_logs))

    assert result.classification == "clob_atomic"
    assert result.wallet is None
    assert result.counterparty == exchange

    ambiguous = classify_receipt(
        _candidate("split"),
        _receipt(base_logs + [_batch_log(26, ADAPTER, SAFE)]),
    )
    assert ambiguous.classification == "unresolved"
    assert ambiguous.reason == "ambiguous_counterparty_transfer"

    boolean_status = _receipt(base_logs)
    boolean_status["status"] = True
    assert classify_receipt(_candidate("split"), boolean_status).reason == "receipt_not_successful"

    bad_padding = _batch_log(22, ADAPTER, exchange)
    bad_topics = bad_padding["topics"]
    assert isinstance(bad_topics, list)
    bad_topics[1] = "0x" + "01" * 12 + OPERATOR[2:]
    malformed = classify_receipt(
        _candidate("split"),
        _receipt([_batch_log(20, ZERO_ADDRESS, ADAPTER), bad_padding]),
    )
    assert malformed.classification == "unresolved"
    assert malformed.reason == "malformed_receipt"

    successful_hash, failed_hash = "0x" + "11" * 32, "0x" + "22" * 32
    cached_receipt: dict[str, object] = {
        "status": "0x1", "transactionHash": successful_hash, "logs": []
    }

    def fetch(_rpc_url: str, tx_hash: str, _timeout: float, _attempts: int) -> dict[str, object]:
        if tx_hash == failed_hash:
            raise ReceiptFetchError("bounded failure")
        return cached_receipt

    cache = tmp_path / "receipts.jsonl"
    with patch("tools.adapter_receipt_attributor._fetch_receipt", side_effect=fetch):
        with pytest.raises(ReceiptFetchError, match="failed receipt hashes"):
            _fetch_missing("unused", [successful_hash, failed_hash], cache,
                           workers=2, timeout=1, attempts=1)
    assert set(_load_cache(cache)) == {successful_hash}

    def digest(value: object) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode()
        return hashlib.sha256(encoded).hexdigest()

    mapping = [{
        "asset": "btc", "condition_id": CONDITION, "down_token": str(TOKENS[1]),
        "slug": "btc-updown-5m-audit", "start": BLOCK_TIMESTAMP,
        "up_token": str(TOKENS[0]), "winner_up": 1,
    }]
    candidate_query = {"schema": "bounded-query-v1", "sql": "SELECT bounded_candidates"}
    artifact: dict[str, Any] = {
        "schema": CANDIDATE_SCHEMA,
        "query": {
            "candidate_query_sha256": digest(candidate_query), "chain_id": 137,
            "cohort_sha256": "00" * 32, "end_block": BLOCK + 1,
            "lifecycle_end_exclusive": BLOCK_TIMESTAMP + 1,
            "lifecycle_start": BLOCK_TIMESTAMP,
            "market_mapping_sha256": digest(mapping), "start_block": BLOCK,
        },
        "candidate_query": candidate_query, "market_mapping": mapping,
        "counts": {"candidates": 1}, "candidates": [_candidate("split").as_json()],
    }
    artifact_path = tmp_path / "candidates.json"

    def load(value: dict[str, Any]) -> None:
        artifact_path.write_text(json.dumps(value), encoding="utf-8")
        load_input(artifact_path, 4_000)

    load(artifact)
    for field, value, message in (
        ("source_block_number", BLOCK + 1, "source block"),
        ("source_block_timestamp", BLOCK_TIMESTAMP + 1, "source timestamp"),
    ):
        invalid = copy.deepcopy(artifact)
        invalid["candidates"][0][field] = value
        with pytest.raises(InputError, match=message):
            load(invalid)
    invalid = copy.deepcopy(artifact)
    invalid["candidate_query"]["sql"] += " changed"
    with pytest.raises(InputError, match="candidate_query SHA-256"):
        load(invalid)
    invalid = copy.deepcopy(artifact)
    invalid["market_mapping"][0]["slug"] = "changed"
    with pytest.raises(InputError, match="market_mapping SHA-256"):
        load(invalid)
    invalid = copy.deepcopy(artifact)
    invalid["candidates"][0]["token_ids"] = [str(TOKENS[0]), "999"]
    with pytest.raises(InputError, match="do not match market_mapping"):
        load(invalid)
    invalid = copy.deepcopy(artifact)
    invalid["counts"]["candidates"] = 2
    with pytest.raises(InputError, match="counts.candidates"):
        load(invalid)
