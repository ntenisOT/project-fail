"""Attribute bounded adapter split/merge candidates from Polygon receipts.

Input carries an auditable query and bounded candidates; output is immutable.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import TYPE_CHECKING, Mapping, Sequence
import urllib.request

if TYPE_CHECKING:
    from tools.adapter_receipt_core import classify_receipt
    from tools.adapter_receipt_input import InputError, load_input
elif __package__:
    from .adapter_receipt_core import classify_receipt
    from .adapter_receipt_input import InputError, load_input
else:
    from adapter_receipt_core import classify_receipt
    from adapter_receipt_input import InputError, load_input

CACHE_SCHEMA = "project-fail-polygon-receipt-cache-v1"
OUTPUT_SCHEMA = "project-fail-adapter-receipt-attribution-v1"
HARD_MAX_CANDIDATES = 10_000


class ReceiptFetchError(RuntimeError):
    """A receipt could not be fetched within the configured retry bound."""


def _reject_nonfinite(value: str) -> None:
    raise InputError(f"non-finite JSON number is forbidden: {value}")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _receipt_hash(receipt: Mapping[str, object]) -> str:
    return _sha256(_canonical(receipt))


def _tx_hash(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 66:
        raise InputError("transaction hash must be 32-byte 0x hex")
    normalized = value.lower()
    if any(char not in "0123456789abcdef" for char in normalized[2:]):
        raise InputError("transaction hash must be 32-byte 0x hex")
    return normalized


def _load_cache(path: Path) -> dict[str, dict[str, object]]:
    receipts: dict[str, dict[str, object]] = {}
    if not path.exists():
        return receipts
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise InputError(f"blank cache row at line {line_number}")
            try:
                row = json.loads(line, parse_constant=_reject_nonfinite)
            except json.JSONDecodeError as exc:
                raise InputError(f"invalid cache JSON at line {line_number}") from exc
            if not isinstance(row, Mapping) or row.get("schema") != CACHE_SCHEMA:
                raise InputError(f"invalid cache schema at line {line_number}")
            tx_hash = _tx_hash(row.get("tx_hash"))
            receipt = row.get("receipt")
            digest = row.get("receipt_sha256")
            if not isinstance(receipt, dict):
                raise InputError(f"invalid cache fields at line {line_number}")
            actual = _receipt_hash(receipt)
            if digest != actual:
                raise InputError(f"cache digest mismatch at line {line_number}")
            if _tx_hash(receipt.get("transactionHash")) != tx_hash:
                raise InputError(f"cache transaction hash mismatch at line {line_number}")
            prior = receipts.get(tx_hash)
            if prior is not None and _receipt_hash(prior) != actual:
                raise InputError(f"conflicting cached receipts for {tx_hash}")
            receipts[tx_hash] = receipt
    return receipts


def _append_cache(path: Path, receipts: Mapping[str, Mapping[str, object]]) -> None:
    if not receipts:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for tx_hash in sorted(receipts):
            receipt = receipts[tx_hash]
            row = {
                "schema": CACHE_SCHEMA,
                "tx_hash": tx_hash,
                "receipt_sha256": _receipt_hash(receipt),
                "receipt": receipt,
            }
            handle.write(_canonical(row).decode("utf-8") + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _fetch_receipt(rpc_url: str, tx_hash: str, timeout: float, attempts: int) -> dict[str, object]:
    request_body = _canonical({"jsonrpc": "2.0", "id": 1,
                               "method": "eth_getTransactionReceipt", "params": [tx_hash]})
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(rpc_url, data=request_body, method="POST", headers={
                "Content-Type": "application/json", "User-Agent": "project-fail-receipt-attributor/1"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response, parse_constant=_reject_nonfinite)
            if not isinstance(payload, Mapping) or payload.get("error") is not None:
                raise ReceiptFetchError("JSON-RPC returned an error")
            receipt = payload.get("result")
            if not isinstance(receipt, dict):
                raise ReceiptFetchError("receipt is not yet available")
            if receipt.get("transactionHash", "").lower() != tx_hash:
                raise ReceiptFetchError("receipt transaction hash mismatch")
            return receipt
        except Exception as exc:
            if attempt + 1 == attempts:
                raise ReceiptFetchError(f"receipt fetch failed for {tx_hash} after {attempts} attempts") from exc
            time.sleep(min(0.25 * (2**attempt), 2.0))
    raise AssertionError("unreachable")


def _fetch_missing(
    rpc_url: str,
    tx_hashes: Sequence[str],
    cache_path: Path,
    *,
    workers: int,
    timeout: float,
    attempts: int,
) -> dict[str, dict[str, object]]:
    fetched: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(tx_hashes))) as pool:
        future_to_hash = {
            pool.submit(_fetch_receipt, rpc_url, tx_hash, timeout, attempts): tx_hash
            for tx_hash in tx_hashes
        }
        for future in as_completed(future_to_hash):
            tx_hash = future_to_hash[future]
            try:
                fetched[tx_hash] = future.result()
            except ReceiptFetchError:
                errors.append(tx_hash)
    _append_cache(cache_path, fetched)
    if errors:
        joined = ", ".join(sorted(errors)[:5])
        suffix = " ..." if len(errors) > 5 else ""
        raise ReceiptFetchError(f"failed receipt hashes ({len(errors)}): {joined}{suffix}")
    return fetched


def _git_head(repo: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("unable to resolve git revision") from exc


def _revision(repo: Path) -> dict[str, object]:
    source_paths = [Path(__file__).with_name(name).resolve() for name in (
        "adapter_receipt_attributor.py", "adapter_receipt_core.py", "adapter_receipt_input.py")]
    source_hashes = {path.name: _sha256(path.read_bytes()) for path in sorted(source_paths)}
    git_head = _git_head(repo)
    revision_hash = _sha256(_canonical({"git_head": git_head, "source_sha256": source_hashes}))
    return {"git_head": git_head, "source_sha256": source_hashes, "revision_sha256": revision_hash}


def _write_immutable(path: Path, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return _sha256(encoded)


def run(args: argparse.Namespace) -> tuple[Path, str, dict[str, int]]:
    repo = Path(__file__).resolve().parents[1]
    start_revision = _revision(repo)
    resolved_paths = {args.input.resolve(), args.cache.resolve(), args.output.resolve()}
    if len(resolved_paths) != 3:
        raise InputError("input, cache, and output paths must be distinct")
    if args.output.exists():
        raise FileExistsError(f"immutable output already exists: {args.output}")
    candidates, query, input_hash, query_hash = load_input(args.input, args.max_candidates)
    receipts = _load_cache(args.cache)
    tx_hashes = sorted({candidate.tx_hash for candidate in candidates})
    missing = [tx_hash for tx_hash in tx_hashes if tx_hash not in receipts]
    rpc_url_used: str | None = None
    if missing:
        rpc_url = args.rpc_url or os.environ.get("POLYGON_RPC_URL")
        if not rpc_url:
            raise ReceiptFetchError("RPC URL required for uncached receipts")
        rpc_url_used = rpc_url
        fetched = _fetch_missing(
            rpc_url,
            missing,
            args.cache,
            workers=args.workers,
            timeout=args.timeout_s,
            attempts=args.attempts,
        )
        receipts.update(fetched)
    results: list[dict[str, object]] = []
    counts = {"clob_atomic": 0, "explicit_wallet": 0, "unresolved": 0}
    for candidate in candidates:
        receipt = receipts[candidate.tx_hash]
        attribution = classify_receipt(
            candidate,
            receipt,
            amount_tolerance=args.amount_tolerance_base_units,
        )
        counts[attribution.classification] += 1
        results.append(
            {
                "candidate": candidate.as_json(),
                "attribution": attribution.as_json(),
                "receipt_sha256": _receipt_hash(receipt),
            }
        )
    if _revision(repo) != start_revision:
        raise RuntimeError("source or Git revision changed during attribution; output refused")
    payload: dict[str, object] = {
        "schema": OUTPUT_SCHEMA,
        "input_sha256": input_hash,
        "query": query,
        "query_sha256": query_hash,
        "revision": start_revision,
        "rpc_endpoint_sha256": _sha256((rpc_url_used or "cache-only").encode()),
        "settings": {
            "amount_tolerance_base_units": args.amount_tolerance_base_units,
            "attempts": args.attempts,
            "max_candidates": args.max_candidates,
            "timeout_s": args.timeout_s,
            "workers": args.workers,
        },
        "limitations": [
            "token_ids are trusted candidate inputs and are not re-derived from condition_id",
            "explicit_wallet identifies the receipt counterparty, not Safe bytecode or beneficial ownership",
            "clob_atomic still needs same-transaction maker-leg joins to attribute a trading wallet",
            "the CLOB classification pins the two current V2 exchange addresses in this revision",
            "ambiguous same-token same-amount paths are unresolved",
            "receipt finality and candidate provenance remain caller responsibilities",
            "the append-only cache assumes one writer at a time",
        ],
        "counts": {"candidates": len(candidates), "transactions": len(tx_hashes), **counts},
        "results": results,
    }
    digest = _write_immutable(args.output, payload)
    return args.output, digest, counts


def _bounded_int(name: str, minimum: int, maximum: int):
    def parse(value: str) -> int:
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(f"{name} must be in [{minimum}, {maximum}]")
        return parsed

    return parse


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rpc-url", help="Polygon JSON-RPC URL; never written to output")
    parser.add_argument("--max-candidates", type=_bounded_int("max-candidates", 1, HARD_MAX_CANDIDATES), default=4_000)
    parser.add_argument("--workers", type=_bounded_int("workers", 1, 32), default=8)
    parser.add_argument("--attempts", type=_bounded_int("attempts", 1, 5), default=3)
    parser.add_argument("--timeout-s", type=float, default=15.0)
    parser.add_argument("--amount-tolerance-base-units", type=_bounded_int("amount tolerance", 0, 1_000), default=0)
    args = parser.parse_args(argv)
    if not 0 < args.timeout_s <= 30:
        parser.error("--timeout-s must be in (0, 30]")
    try:
        output, digest, counts = run(args)
    except (InputError, ReceiptFetchError, FileExistsError, OSError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps({"output": str(output), "output_sha256": digest, "counts": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
