"""Freeze and validate authoritative CTF payout vectors for mapped windows."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
from typing import Mapping, Sequence

from eth_utils import keccak

from tools.market_windows import ResolvedWindow
from tools.mint_accounting_inputs import (
    EvidenceError,
    canonical,
    digest,
    integer,
    load_json,
    mapping,
    revision,
    sha256,
    validated_revision_manifest,
)
from tools.mint_ctf_rpc import (
    CHAIN_ID,
    CONDITION_RESOLUTION_TOPIC,
    CTF,
    abi_call as _call,
    batch_calls as _batch_calls,
    resolution_logs as _resolution_logs,
    rpc as _rpc,
)


SCHEMA = "project-fail-ctf-payout-evidence-v1"
POSITION_COLLATERAL = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"


def _resolution_record(
    condition: str, raw: Mapping[str, object], expected: tuple[int, int],
) -> dict[str, object]:
    topics = raw.get("topics")
    data = str(raw.get("data") or "").lower().removeprefix("0x")
    if (str(raw.get("address") or "").lower() != CTF
            or raw.get("removed") is not False
            or not isinstance(topics, list) or len(topics) != 4
            or any(not isinstance(topic, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", topic)
                   for topic in topics)
            or str(topics[0]).lower() != CONDITION_RESOLUTION_TOPIC
            or str(topics[1]).lower() != condition
            or len(data) != 5 * 64
            or any(char not in "0123456789abcdef" for char in data)):
        raise EvidenceError("ConditionResolution log is malformed")
    words = [int(data[index:index + 64], 16) for index in range(0, len(data), 64)]
    if words[:3] != [2, 64, 2] or tuple(words[3:]) != expected:
        raise EvidenceError("ConditionResolution payout vector differs from CTF state")
    oracle_topic = str(topics[2]).lower()
    if oracle_topic[2:26] != "0" * 24:
        raise EvidenceError("ConditionResolution oracle topic is not an indexed address")
    oracle = "0x" + oracle_topic[-40:]
    question_id = str(topics[3]).lower()
    derived = "0x" + keccak(
        bytes.fromhex(oracle[2:]) + bytes.fromhex(question_id[2:]) + (2).to_bytes(32, "big")
    ).hex()
    if derived != condition:
        raise EvidenceError("ConditionResolution indexed fields do not derive the condition")
    block_hash = str(raw.get("blockHash") or "").lower()
    tx_hash = str(raw.get("transactionHash") or "").lower()
    if (not re.fullmatch(r"0x[0-9a-f]{64}", block_hash)
            or not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash)):
        raise EvidenceError("ConditionResolution lacks canonical block or transaction hashes")
    try:
        block_number = int(str(raw.get("blockNumber")), 16)
        log_index = int(str(raw.get("logIndex")), 16)
    except ValueError as exc:
        raise EvidenceError("ConditionResolution ordering fields are malformed") from exc
    return _verified_resolution_record(condition, {
        "block_number": block_number,
        "block_hash": block_hash,
        "log_index": log_index,
        "tx_hash": tx_hash,
        "oracle": oracle,
        "question_id": question_id,
        "outcome_slot_count": 2,
        "payout_numerators": [str(value) for value in expected],
    }, expected)


def _verified_resolution_record(
    condition: str, value: Mapping[str, object], expected: tuple[int, int],
) -> dict[str, object]:
    oracle = str(value.get("oracle") or "").lower()
    question_id = str(value.get("question_id") or "").lower()
    block_hash = str(value.get("block_hash") or "").lower()
    tx_hash = str(value.get("tx_hash") or "").lower()
    if (not re.fullmatch(r"0x[0-9a-f]{40}", oracle)
            or not re.fullmatch(r"0x[0-9a-f]{64}", question_id)
            or not re.fullmatch(r"0x[0-9a-f]{64}", block_hash)
            or not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash)
            or value.get("outcome_slot_count") != 2
            or _exact_pair(value.get("payout_numerators"), "resolution payout") != expected):
        raise EvidenceError("frozen ConditionResolution proof is malformed")
    derived = "0x" + keccak(
        bytes.fromhex(oracle[2:]) + bytes.fromhex(question_id[2:]) + (2).to_bytes(32, "big")
    ).hex()
    if derived != condition:
        raise EvidenceError("frozen ConditionResolution proof derives another condition")
    return {
        "block_number": integer(value.get("block_number"), "resolution block"),
        "block_hash": block_hash,
        "log_index": integer(value.get("log_index"), "resolution log index"),
        "tx_hash": tx_hash,
        "oracle": oracle,
        "question_id": question_id,
        "outcome_slot_count": 2,
        "payout_numerators": [str(item) for item in expected],
    }


def _validate_window(
    window: ResolvedWindow,
    denominator: int,
    numerators: tuple[int, int],
    position_ids: tuple[int, int],
) -> dict[str, object]:
    if denominator <= 0 or sum(numerators) != denominator:
        raise EvidenceError("CTF payout vector is unresolved or does not sum to its denominator")
    if sorted(numerators) != [0, denominator]:
        raise EvidenceError("binary CTF payout is not an unambiguous single winner")
    expected = {int(window.up_token), int(window.down_token)}
    if set(position_ids) != expected:
        raise EvidenceError("on-chain position IDs do not match the frozen token mapping")
    up_index = position_ids.index(int(window.up_token))
    winner_index = numerators.index(denominator)
    winner_up = int(winner_index == up_index)
    if winner_up != window.winner_up:
        raise EvidenceError("on-chain CTF payout contradicts the frozen Gamma outcome")
    return {
        "condition_id": window.condition_id.lower(),
        "slug": window.slug,
        "payout_denominator": str(denominator),
        "payout_numerators": [str(value) for value in numerators],
        "position_ids_by_outcome_index": [str(value) for value in position_ids],
        "up_outcome_index": up_index,
        "down_outcome_index": 1 - up_index,
        "winner_outcome_index": winner_index,
        "winner_up": winner_up,
    }


def _exact_pair(value: object, field: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise EvidenceError(f"{field} must contain exactly two integers")
    try:
        pair = (int(str(value[0])), int(str(value[1])))
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"{field} must contain exactly two integers") from exc
    if min(pair) < 0:
        raise EvidenceError(f"{field} must contain non-negative integers")
    return pair


def _block_identity(value: object, field: str) -> tuple[int, str, int]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"Polygon RPC returned no {field} block")
    raw_number = value.get("number")
    raw_hash = str(value.get("hash") or "").lower()
    raw_timestamp = value.get("timestamp")
    try:
        if not isinstance(raw_number, str) or not isinstance(raw_timestamp, str):
            raise ValueError
        number = int(raw_number, 16)
        timestamp = int(raw_timestamp, 16)
    except ValueError as exc:
        raise EvidenceError(f"Polygon RPC returned a malformed {field} block") from exc
    if (number <= 0 or timestamp <= 0
            or not re.fullmatch(r"0x[0-9a-f]{64}", raw_hash)):
        raise EvidenceError(f"Polygon RPC returned a malformed {field} block")
    return number, raw_hash, timestamp


def fetch_evidence(
    windows: Sequence[ResolvedWindow],
    candidate_sha: str,
    mapping_sha: str,
    resolution_from_block: int,
    rpc_url: str,
    *,
    timeout_s: float,
    attempts: int,
) -> dict[str, object]:
    if not windows:
        raise EvidenceError("cannot fetch payouts for an empty mapping")
    chain = _rpc(rpc_url, "eth_chainId", [], timeout_s, attempts)
    if not isinstance(chain, str) or int(chain, 16) != CHAIN_ID:
        raise EvidenceError("Polygon payout RPC is on the wrong chain")
    block = _rpc(rpc_url, "eth_getBlockByNumber", ["finalized", False], timeout_s, attempts)
    block_number, block_hash, block_timestamp = _block_identity(block, "finalized")
    block_tag = hex(block_number)
    block_reference: Mapping[str, object] = {
        "blockHash": block_hash,
        "requireCanonical": True,
    }
    stage_one: dict[str, str] = {}
    for window in windows:
        condition = window.condition_id.lower()
        stage_one[f"{condition}:denominator"] = _call("payoutDenominator(bytes32)", condition)
        for index in (0, 1):
            stage_one[f"{condition}:numerator:{index}"] = _call(
                "payoutNumerators(bytes32,uint256)", condition, index
            )
            stage_one[f"{condition}:collection:{index}"] = _call(
                "getCollectionId(bytes32,bytes32,uint256)", "0x" + "0" * 64,
                condition, 1 << index,
            )
    first = _batch_calls(rpc_url, stage_one, block_reference, timeout_s, attempts)
    position_calls: dict[str, str] = {}
    for window in windows:
        condition = window.condition_id.lower()
        for index in (0, 1):
            position_calls[f"{condition}:position:{index}"] = _call(
                "getPositionId(address,bytes32)",
                POSITION_COLLATERAL,
                first[f"{condition}:collection:{index}"],
            )
    positions = _batch_calls(rpc_url, position_calls, block_reference, timeout_s, attempts)
    resolution_logs = _resolution_logs(
        rpc_url,
        [window.condition_id.lower() for window in windows],
        resolution_from_block,
        block_number,
        timeout_s,
        attempts,
    )
    final_check = _rpc(
        rpc_url, "eth_getBlockByNumber", [block_tag, False], timeout_s, attempts
    )
    if _block_identity(final_check, "canonical recheck") != (
        block_number, block_hash, block_timestamp,
    ):
        raise EvidenceError("Polygon finalized block changed during payout collection")
    rows = []
    for window in windows:
        condition = window.condition_id.lower()
        numerators = (
            int(first[f"{condition}:numerator:0"], 16),
            int(first[f"{condition}:numerator:1"], 16),
        )
        row = _validate_window(
            window,
            int(first[f"{condition}:denominator"], 16),
            numerators,
            (int(positions[f"{condition}:position:0"], 16),
             int(positions[f"{condition}:position:1"], 16)),
        )
        row["condition_resolution"] = _resolution_record(
            condition, resolution_logs[condition], numerators
        )
        rows.append(row)
    return {
        "schema": SCHEMA,
        "source": {"candidate_sha256": candidate_sha, "market_mapping_sha256": mapping_sha},
        "chain": {
            "chain_id": CHAIN_ID,
            "conditional_tokens": CTF,
            "position_id_collateral": POSITION_COLLATERAL,
            "block_number": block_number,
            "block_hash": block_hash,
            "block_timestamp": block_timestamp,
            "finality_tag": "finalized",
            "state_call_block_reference": "eip-1898-blockHash-requireCanonical",
            "rpc_endpoint_sha256": hashlib.sha256(rpc_url.encode()).hexdigest(),
        },
        "rows": rows,
    }


def verified_winners(
    payload: Mapping[str, object],
    candidate_sha: str,
    mapping_sha: str,
    windows: Sequence[ResolvedWindow],
    expected_revision: Mapping[str, object],
) -> dict[str, bool]:
    source, chain, rows = payload.get("source"), payload.get("chain"), payload.get("rows")
    if (payload.get("schema") != SCHEMA or not isinstance(source, Mapping)
            or source.get("candidate_sha256") != candidate_sha
            or source.get("market_mapping_sha256") != mapping_sha
            or not isinstance(chain, Mapping) or chain.get("chain_id") != CHAIN_ID
            or str(chain.get("conditional_tokens") or "").lower() != CTF
            or str(chain.get("position_id_collateral") or "").lower() != POSITION_COLLATERAL
            or chain.get("finality_tag") != "finalized"
            or chain.get("state_call_block_reference") != "eip-1898-blockHash-requireCanonical"
            or not isinstance(rows, list)):
        raise EvidenceError("CTF payout artifact provenance is invalid")
    producer_revision = validated_revision_manifest(payload.get("revision"))
    if producer_revision != dict(expected_revision):
        raise EvidenceError("CTF payout producer revision differs from the accounting revision")
    digest(str(chain.get("rpc_endpoint_sha256")), "payout RPC endpoint SHA-256")
    expected = {window.condition_id.lower(): window for window in windows}
    result: dict[str, bool] = {}
    frozen_block = integer(chain.get("block_number"), "payout evidence block")
    if (not re.fullmatch(r"0x[0-9a-f]{64}", str(chain.get("block_hash") or "").lower())
            or integer(chain.get("block_timestamp"), "payout evidence timestamp") == 0):
        raise EvidenceError("CTF payout artifact lacks a canonical frozen block")
    resolution_keys: set[tuple[int, int]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise EvidenceError("CTF payout row is malformed")
        condition = str(row.get("condition_id") or "").lower()
        window = expected.get(condition)
        if window is None or condition in result or row.get("slug") != window.slug:
            raise EvidenceError("CTF payout row escapes or duplicates the mapping")
        validated = _validate_window(
            window,
            int(str(row.get("payout_denominator"))),
            _exact_pair(row.get("payout_numerators"), "payout_numerators"),
            _exact_pair(
                row.get("position_ids_by_outcome_index"),
                "position_ids_by_outcome_index",
            ),
        )
        raw_resolution = row.get("condition_resolution")
        if not isinstance(raw_resolution, Mapping):
            raise EvidenceError("CTF payout row lacks its ConditionResolution proof")
        replayed_resolution = _verified_resolution_record(
            condition,
            raw_resolution,
            _exact_pair(row.get("payout_numerators"), "payout_numerators"),
        )
        if dict(raw_resolution) != replayed_resolution:
            raise EvidenceError("ConditionResolution proof has inconsistent derived fields")
        resolution_key = (
            integer(replayed_resolution["block_number"], "resolution block"),
            integer(replayed_resolution["log_index"], "resolution log index"),
        )
        if resolution_key[0] > frozen_block or resolution_key in resolution_keys:
            raise EvidenceError("ConditionResolution proof is after the frozen block or duplicated")
        resolution_keys.add(resolution_key)
        if any(row.get(field) != validated[field] for field in (
            "up_outcome_index", "down_outcome_index", "winner_outcome_index", "winner_up"
        )):
            raise EvidenceError("CTF payout row derived fields are inconsistent")
        result[condition] = bool(validated["winner_up"])
    if set(result) != set(expected):
        raise EvidenceError("CTF payout artifact does not cover every mapped condition")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--rpc-url")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=15.0)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        candidate_sha = digest(args.candidate_sha256, "candidate SHA-256")
        candidate = load_json(
            args.candidate, candidate_sha, "project-fail-adapter-receipt-candidates-v1"
        )
        windows, query = mapping(candidate)
        mapping_sha = digest(str(query.get("market_mapping_sha256")), "mapping SHA-256")
        rpc_url = (
            args.rpc_url or os.environ.get("POLYGON_RPC_URL")
            or "https://polygon.publicnode.com"
        )
        repo = Path(__file__).resolve().parents[1]
        start_revision = revision(repo)
        payload = fetch_evidence(
            windows, candidate_sha, mapping_sha,
            integer(query.get("start_block"), "candidate start block"), rpc_url,
            timeout_s=args.timeout_s, attempts=args.attempts,
        )
        payload["revision"] = start_revision
        if start_revision != revision(repo) or sha256(args.candidate.read_bytes()) != candidate_sha:
            raise EvidenceError("source revision or candidate changed during payout collection")
        raw = canonical(payload) + b"\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as handle:
            handle.write(raw)
    except (EvidenceError, OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"wrote {args.output} sha256={sha256(raw)} rows={len(windows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
