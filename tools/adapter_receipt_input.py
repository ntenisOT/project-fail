"""Fail-closed validation for receipt-candidate artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from tools.adapter_receipt_core import Candidate
elif __package__:
    from .adapter_receipt_core import Candidate
else:
    from adapter_receipt_core import Candidate

CANDIDATE_SCHEMA = "project-fail-adapter-receipt-candidates-v1"


class InputError(ValueError):
    """The bounded input or cache violates its declared schema."""


def _reject_nonfinite(value: str) -> None:
    raise InputError(f"non-finite JSON number is forbidden: {value}")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: object, field: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value.lower())):
        raise InputError(f"input.query.{field} must be 64 hex digits")
    return value.lower()


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"input.query.{field} must be an integer")
    return value


def _token(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise InputError(f"market_mapping.{field} must be uint256")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value and value.isdecimal():
        result = int(value)
    else:
        raise InputError(f"market_mapping.{field} must be uint256")
    if not 0 <= result < 2**256:
        raise InputError(f"market_mapping.{field} must be uint256")
    return result


def _market_mapping(payload: Mapping[str, object], expected_hash: str) -> dict[str, tuple[int, int]]:
    raw = payload.get("market_mapping")
    if not isinstance(raw, list) or not raw:
        raise InputError("input.market_mapping must be a non-empty list")
    if _sha256(_canonical(raw)) != expected_hash:
        raise InputError("embedded market_mapping SHA-256 mismatch")
    result: dict[str, tuple[int, int]] = {}
    seen_tokens: set[int] = set()
    for index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise InputError(f"market_mapping row {index} must be an object")
        condition = row.get("condition_id")
        if (not isinstance(condition, str) or len(condition) != 66 or not condition.startswith("0x")
                or any(char not in "0123456789abcdef" for char in condition[2:].lower())):
            raise InputError(f"market_mapping row {index} has invalid condition_id")
        condition = condition.lower()
        tokens = (_token(row.get("up_token"), "up_token"),
                  _token(row.get("down_token"), "down_token"))
        if tokens[0] == tokens[1] or condition in result or seen_tokens.intersection(tokens):
            raise InputError("market_mapping has duplicate condition or token pair")
        result[condition] = tokens
        seen_tokens.update(tokens)
    return result


def load_input(path: Path, max_candidates: int) -> tuple[list[Candidate], object, str, str]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw, parse_constant=_reject_nonfinite)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InputError("input is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping) or payload.get("schema") != CANDIDATE_SCHEMA:
        raise InputError(f"input schema must be {CANDIDATE_SCHEMA}")
    query = payload.get("query")
    if not isinstance(query, Mapping):
        raise InputError("input.query must be an object")
    if _integer(query.get("chain_id"), "chain_id") != 137:
        raise InputError("input.query.chain_id must be integer 137")
    start_block = _integer(query.get("start_block"), "start_block")
    end_block = _integer(query.get("end_block"), "end_block")
    start_time = _integer(query.get("lifecycle_start"), "lifecycle_start")
    end_time = _integer(query.get("lifecycle_end_exclusive"), "lifecycle_end_exclusive")
    if start_block < 0 or start_block >= end_block or start_time < 0 or start_time >= end_time:
        raise InputError("input.query block or timestamp bounds are invalid")
    _digest(query.get("cohort_sha256"), "cohort_sha256")
    mapping_hash = _digest(query.get("market_mapping_sha256"), "market_mapping_sha256")
    candidate_query_hash = _digest(query.get("candidate_query_sha256"), "candidate_query_sha256")
    candidate_query = payload.get("candidate_query")
    if not isinstance(candidate_query, Mapping) or _sha256(_canonical(candidate_query)) != candidate_query_hash:
        raise InputError("embedded candidate_query SHA-256 mismatch")
    mapping = _market_mapping(payload, mapping_hash)
    rows = payload.get("candidates")
    counts = payload.get("counts")
    if not isinstance(rows, list) or not rows:
        raise InputError("input.candidates must be a non-empty list")
    if len(rows) > max_candidates:
        raise InputError(f"candidate count {len(rows)} exceeds bound {max_candidates}")
    declared_count = counts.get("candidates") if isinstance(counts, Mapping) else None
    if isinstance(declared_count, bool) or not isinstance(declared_count, int) or declared_count != len(rows):
        raise InputError("input.counts.candidates does not match candidate rows")
    candidates: list[Candidate] = []
    seen: set[Candidate] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise InputError(f"candidate {index} must be an object")
        try:
            candidate = Candidate.from_mapping(row)
        except ValueError as exc:
            raise InputError(f"candidate {index}: {exc}") from exc
        if not start_block <= candidate.source_block_number < end_block:
            raise InputError(f"candidate {index} source block is outside query bounds")
        if not start_time <= candidate.source_block_timestamp < end_time:
            raise InputError(f"candidate {index} source timestamp is outside query bounds")
        if mapping.get(candidate.condition_id) != candidate.token_ids:
            raise InputError(f"candidate {index} condition/token IDs do not match market_mapping")
        if candidate in seen:
            raise InputError(f"candidate {index} duplicates an earlier candidate")
        seen.add(candidate)
        candidates.append(candidate)
    return candidates, query, _sha256(raw), _sha256(_canonical(query))
