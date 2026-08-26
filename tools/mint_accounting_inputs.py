"""Strict input and provenance gates for the mint accounting artifact."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping

from tools.market_windows import ResolvedWindow
from tools.evidence_provenance import ATTRIBUTION_PRODUCER_PATHS, CANDIDATE_PRODUCER_PATHS


WALLET_RE = re.compile(r"^0x[0-9a-f]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
MONEY_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")
SOURCE_PATHS = tuple(dict.fromkeys((
    "tools/mint_accounting.py", "tools/mint_accounting_capital.py",
    "tools/mint_cohort_aggregate.py",
    "tools/mint_accounting_clickhouse.py", "tools/mint_accounting_core.py",
    "tools/mint_accounting_inputs.py", "tools/mint_accounting_outcomes.py",
    "tools/mint_attribution_validation.py",
    "tools/mint_ctf_rpc.py",
    "tools/mint_sibling_cohort.py", "research/mint_sibling_cohort_v1.json",
    "tools/market_windows.py", "tools/clickhouse_forensics.py",
    "tools/wallet_metrics.py", "requirements.txt",
    *CANDIDATE_PRODUCER_PATHS,
    *ATTRIBUTION_PRODUCER_PATHS,
)))


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
                    receipt = row.get("receipt")
                    if not isinstance(receipt, Mapping):
                        raise EvidenceError("target receipt cache row lacks its receipt")
                    declared = digest(str(row.get("receipt_sha256")), "receipt_sha256")
                    if (sha256(canonical(receipt)) != declared
                            or str(receipt.get("transactionHash") or "").lower() != tx_hash):
                        raise EvidenceError("target cached receipt content or transaction is invalid")
                    if tx_hash in found:
                        raise EvidenceError("target receipt cache transaction is duplicated")
                    found[tx_hash] = declared
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
    dirty = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    if dirty:
        raise EvidenceError("the repository is dirty; exact accounting requires a clean revision")
    hashes = {name: hash_file(repo / name) for name in SOURCE_PATHS}
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip().lower()
    if not GIT_RE.fullmatch(head):
        raise EvidenceError("git HEAD is not a canonical revision")
    try:
        runtime = {
            "python_implementation": sys.implementation.name,
            "python_version": ".".join(str(value) for value in sys.version_info[:3]),
            "clickhouse_connect_version": version("clickhouse-connect"),
            "eth_utils_version": version("eth-utils"),
        }
    except PackageNotFoundError as exc:
        raise EvidenceError("an exact accounting runtime dependency is unavailable") from exc
    frozen = {"git_head": head, "source_sha256": hashes, "runtime": runtime}
    return {**frozen, "revision_sha256": sha256(canonical(frozen))}


def validated_revision_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise EvidenceError("producer revision manifest is missing")
    head = str(value.get("git_head") or "").lower()
    raw_sources, raw_runtime = value.get("source_sha256"), value.get("runtime")
    if (not GIT_RE.fullmatch(head) or not isinstance(raw_sources, Mapping)
            or set(raw_sources) != set(SOURCE_PATHS) or not isinstance(raw_runtime, Mapping)):
        raise EvidenceError("producer revision manifest has an invalid shape")
    sources = {
        name: digest(str(raw_sources[name]), f"producer source {name}")
        for name in SOURCE_PATHS
    }
    required_runtime = (
        "python_implementation", "python_version",
        "clickhouse_connect_version", "eth_utils_version",
    )
    if set(raw_runtime) != set(required_runtime):
        raise EvidenceError("producer runtime manifest has an invalid shape")
    runtime = {name: str(raw_runtime[name]) for name in required_runtime}
    if any(not item for item in runtime.values()):
        raise EvidenceError("producer runtime manifest contains an empty value")
    frozen = {"git_head": head, "source_sha256": sources, "runtime": runtime}
    expected = sha256(canonical(frozen))
    if value.get("revision_sha256") != expected:
        raise EvidenceError("producer revision manifest hash is invalid")
    return {**frozen, "revision_sha256": expected}
