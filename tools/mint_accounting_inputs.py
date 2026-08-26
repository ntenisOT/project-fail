"""Strict input and provenance gates for the mint accounting artifact."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from tools.market_windows import ResolvedWindow


WALLET_RE = re.compile(r"^0x[0-9a-f]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MONEY_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")
STANDARD_ADAPTER = "0xada100db00ca00073811820692005400218fce1f"
SOURCE_NAMES = (
    "mint_accounting.py", "mint_accounting_clickhouse.py",
    "mint_accounting_core.py", "mint_accounting_inputs.py",
)


class EvidenceError(ValueError):
    """Inputs or source coverage cannot support the declared artifact."""


def _reject_constant(value: str) -> None:
    raise EvidenceError(f"non-finite JSON number is forbidden: {value}")


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode()


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest(value: str, field: str) -> str:
    result = value.lower()
    if not SHA_RE.fullmatch(result):
        raise EvidenceError(f"{field} must be 64 lowercase hex digits")
    return result


def load_json(path: Path, expected: str, schema: str) -> dict[str, Any]:
    raw = path.read_bytes()
    if sha256(raw) != digest(expected, f"{path.name} SHA-256"):
        raise EvidenceError(f"SHA-256 mismatch for {path}")
    try:
        payload = json.loads(raw, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid JSON input: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        raise EvidenceError(f"unexpected schema for {path}")
    return payload


def integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise EvidenceError(f"{field} must be an integer")
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"{field} must be an integer") from exc
    if result < 0:
        raise EvidenceError(f"{field} must be non-negative")
    return result


def signed_integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise EvidenceError(f"{field} must be an integer")
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"{field} must be an integer") from exc


def _candidate_key(row: Mapping[str, object]) -> tuple[object, ...]:
    tokens = row.get("token_ids")
    if not isinstance(tokens, list) or len(tokens) != 2:
        raise EvidenceError("candidate token_ids must contain exactly two tokens")
    return (
        str(row.get("adapter") or "").lower(), str(row.get("amount")),
        str(row.get("condition_id") or "").lower(), str(row.get("op") or ""),
        integer(row.get("source_block_number"), "source_block_number"),
        integer(row.get("source_block_timestamp"), "source_block_timestamp"),
        integer(row.get("source_log_index"), "source_log_index"),
        tuple(str(token) for token in tokens), str(row.get("tx_hash") or "").lower(),
    )


def mapping(candidate: Mapping[str, object]) -> tuple[list[ResolvedWindow], dict[str, Any]]:
    rows, query = candidate.get("market_mapping"), candidate.get("query")
    if not isinstance(rows, list) or not isinstance(query, dict):
        raise EvidenceError("candidate artifact lacks mapping or query")
    windows = [ResolvedWindow.from_dict(row) for row in rows if isinstance(row, Mapping)]
    if len(windows) != len(rows) or not windows:
        raise EvidenceError("invalid resolved-window mapping")
    expected = list(range(windows[0].start, windows[-1].start + 1, 300))
    if [window.start for window in windows] != expected:
        raise EvidenceError("resolved-window mapping is not contiguous")
    if sha256(canonical(rows)) != str(query.get("market_mapping_sha256")):
        raise EvidenceError("market mapping hash mismatch")
    return windows, query


def attributed_events(
    candidate: Mapping[str, object], attribution: Mapping[str, object], wallet: str,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    candidates, results, settings = (
        candidate.get("candidates"), attribution.get("results"), attribution.get("settings"),
    )
    if (not isinstance(candidates, list) or not isinstance(results, list)
            or not isinstance(settings, Mapping)
            or settings.get("amount_tolerance_base_units") != 0):
        raise EvidenceError("receipt attribution rows or zero-tolerance settings are missing")
    source = {_candidate_key(row): row for row in candidates if isinstance(row, Mapping)}
    if len(source) != len(candidates):
        raise EvidenceError("candidate rows are malformed or duplicated")
    selected, receipts = [], {}
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("attribution"), dict):
            raise EvidenceError("malformed attribution result")
        proof = result["attribution"]
        addresses = {str(proof.get(name) or "").lower() for name in ("wallet", "counterparty")}
        if wallet not in addresses:
            continue
        if addresses != {wallet} or proof.get("classification") != "explicit_wallet":
            raise EvidenceError("target attribution is not an explicit single-wallet proof")
        row = result.get("candidate")
        if not isinstance(row, dict) or _candidate_key(row) not in source:
            raise EvidenceError("attribution does not rejoin an exact candidate")
        if str(row.get("adapter") or "").lower() != STANDARD_ADAPTER:
            raise EvidenceError("target event is not the standard adapter")
        receipt_hash = digest(str(result.get("receipt_sha256")), "receipt_sha256")
        tx_hash = str(row.get("tx_hash") or "").lower()
        if tx_hash in receipts:
            raise EvidenceError("duplicate target receipt transaction")
        receipts[tx_hash] = receipt_hash
        selected.append(row)
    if not selected:
        raise EvidenceError("no target receipt-attributed lifecycle events")
    return selected, receipts


def verify_receipts(path: Path, expected_sha: str, receipts: Mapping[str, str]) -> None:
    if hash_file(path) != digest(expected_sha, "receipt cache SHA-256"):
        raise EvidenceError("receipt cache SHA-256 mismatch")
    found: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                row = json.loads(raw, parse_constant=_reject_constant)
                tx_hash = str(row.get("tx_hash") or "").lower()
                if tx_hash in receipts:
                    if row.get("schema") != "project-fail-polygon-receipt-cache-v1":
                        raise EvidenceError("target receipt cache schema mismatch")
                    found[tx_hash] = digest(str(row.get("receipt_sha256")), "receipt_sha256")
    except (AttributeError, json.JSONDecodeError) as exc:
        raise EvidenceError("receipt cache contains a malformed row") from exc
    if found != dict(receipts):
        raise EvidenceError("target attribution receipt hashes do not match the frozen cache")


def rebates(
    payload: Mapping[str, object], candidate_sha: str, mapping_sha: str, wallet: str,
    conditions: set[str],
) -> dict[str, int]:
    source, rows = payload.get("source_artifact"), payload.get("selected_rows")
    if (payload.get("maker_address") != wallet or not isinstance(source, dict)
            or source.get("input_sha256") != candidate_sha
            or source.get("market_mapping_sha256") != mapping_sha
            or not isinstance(rows, list)
            or payload.get("mapped_condition_ids_without_response") != []):
        raise EvidenceError("rebate artifact does not bind the full target mapping")
    result: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise EvidenceError("malformed rebate row")
        condition = str(row.get("condition_id") or "").lower()
        amount = row.get("rebated_fees_usdc")
        if (condition not in conditions or condition in result or not isinstance(amount, str)
                or not MONEY_RE.fullmatch(amount)):
            raise EvidenceError("rebate condition or six-decimal amount is invalid")
        try:
            base = Decimal(amount) * 1_000_000
        except InvalidOperation as exc:
            raise EvidenceError("invalid rebate amount") from exc
        if base != base.to_integral_value():
            raise EvidenceError("rebate amount is not exact in base units")
        result[condition] = int(base)
    if set(result) != conditions:
        raise EvidenceError("rebate response does not cover every mapped condition")
    return result


def revision(repo: Path) -> dict[str, object]:
    source_dir = Path(__file__).resolve().parent
    hashes = {name: hash_file(source_dir / name) for name in SOURCE_NAMES}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    return {"git_head": head, "source_sha256": hashes,
            "revision_sha256": sha256(canonical({"git_head": head, "source_sha256": hashes}))}
