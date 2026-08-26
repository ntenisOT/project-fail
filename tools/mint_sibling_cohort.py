#!/usr/bin/env python3
"""Verify the frozen, outcome-independent mint sibling cohort."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Mapping, Sequence

from tools.mint_attribution_validation import validated_attribution_artifact
from tools.mint_accounting_inputs import (
    EvidenceError,
    WALLET_RE,
    canonical,
    digest,
    hash_file,
    integer,
    load_json,
    mapping,
    revision,
    sha256,
    validated_revision_manifest,
    verify_receipts,
)
from tools.mint_falsification_gate import GATE_SPEC


SCHEMA = "project-fail-mint-sibling-cohort-v1"
PROOF_SCHEMA = "project-fail-mint-sibling-cohort-proof-v2"
FROZEN_MANIFEST = Path("research/mint_sibling_cohort_v1.json")
ACCOUNTING_TAIL_S = 24 * 60 * 60
HEX_RE = re.compile(r"^0x[0-9a-f]{64}$")


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{field} must be an object")
    return value


def _candidate_key(row: Mapping[str, object]) -> tuple[object, ...]:
    tokens = row.get("token_ids")
    if (not isinstance(tokens, list) or len(tokens) != 2
            or any(not isinstance(token, str) or not token.isdecimal() for token in tokens)):
        raise EvidenceError("cohort candidate token_ids are malformed")
    tx_hash = str(row.get("tx_hash") or "").lower()
    condition = str(row.get("condition_id") or "").lower()
    if not HEX_RE.fullmatch(tx_hash) or not HEX_RE.fullmatch(condition):
        raise EvidenceError("cohort candidate hashes are malformed")
    return (
        tx_hash,
        condition,
        str(row.get("op") or ""),
        str(row.get("adapter") or "").lower(),
        str(row.get("amount") or ""),
        tuple(tokens),
        integer(row.get("source_block_number"), "source block"),
        integer(row.get("source_block_timestamp"), "source timestamp"),
        integer(row.get("source_log_index"), "source log index"),
    )


def _universe(candidate: Mapping[str, object]) -> list[dict[str, object]]:
    mapping(candidate)
    rows = candidate.get("market_mapping")
    if not isinstance(rows, list):
        raise EvidenceError("cohort candidate lacks a market mapping")
    keys = ("asset", "slug", "start", "condition_id", "up_token", "down_token")
    projection = []
    for row in rows:
        source = _object(row, "market mapping row")
        if any(key not in source for key in keys):
            raise EvidenceError("market mapping row is incomplete")
        projection.append({key: source[key] for key in keys})
    return sorted(projection, key=lambda row: (integer(row["start"], "start"),
                                                str(row["condition_id"])))


def derive(
    candidate: Mapping[str, object],
    attribution: Mapping[str, object],
    manifest: Mapping[str, object],
    candidate_sha: str,
    expected_revision: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    universe = _object(manifest.get("universe"), "manifest universe")
    selection = _object(manifest.get("selection"), "manifest selection")
    selection_lifecycle = _object(
        manifest.get("selection_lifecycle"), "manifest selection_lifecycle"
    )
    expected_grid = _object(manifest.get("expected_grid"), "manifest expected_grid")
    projections = _object(manifest.get("projections"), "manifest projections")
    gate_spec = _object(manifest.get("pre_registered_gate"), "manifest pre_registered_gate")
    if canonical(gate_spec) != canonical(GATE_SPEC):
        raise EvidenceError("cohort falsification gate differs from the frozen contract")
    universe_rows = _universe(candidate)
    candidate_query = _object(candidate.get("query"), "candidate query")
    candidate_query_spec = _object(
        candidate.get("candidate_query"), "candidate query specification"
    )
    mapping_sha = str(candidate_query.get("market_mapping_sha256") or "")
    starts = [integer(row["start"], "start") for row in universe_rows]
    assets = {str(row["asset"]) for row in universe_rows}
    if (mapping_sha != universe.get("market_mapping_sha256")
            or sha256(canonical(universe_rows)) != universe.get("outcome_free_projection_sha256")
            or len(universe_rows) != integer(universe.get("window_count"), "window count")
            or starts[0] != integer(universe.get("start"), "universe start")
            or starts[-1] != integer(universe.get("end"), "universe end")
            or assets != {str(universe.get("asset") or "")}
            or candidate_query.get("chain_id") != universe.get("chain_id")):
        raise EvidenceError("cohort market universe differs from the frozen manifest")
    universe_start = integer(universe.get("start"), "universe start")
    universe_end = integer(universe.get("end"), "universe end")
    selection_start = integer(
        selection_lifecycle.get("lifecycle_start"), "selection lifecycle start"
    )
    selection_end = integer(
        selection_lifecycle.get("lifecycle_end_exclusive"),
        "selection lifecycle end",
    )
    selection_tail = integer(
        selection_lifecycle.get("post_last_market_close_s"),
        "selection post-close tail",
    )
    accounting_end = universe_end + 300 + ACCOUNTING_TAIL_S
    if (
        selection_lifecycle.get("purpose")
        != "historical_membership_reproduction_only"
        or selection_end != universe_end + 300 + selection_tail
        or selection_start >= selection_end
        or integer(candidate_query.get("start"), "candidate query start")
        != universe_start
        or integer(candidate_query.get("end"), "candidate query end") != universe_end
        or integer(candidate_query.get("lifecycle_start"), "candidate lifecycle start")
        != selection_start
        or integer(
            candidate_query.get("lifecycle_end_exclusive"), "candidate lifecycle end"
        )
        != accounting_end
        or integer(
            candidate_query_spec.get("lifecycle_start"),
            "candidate specification lifecycle start",
        )
        != selection_start
        or integer(
            candidate_query_spec.get("lifecycle_end_exclusive"),
            "candidate specification lifecycle end",
        )
        != accounting_end
        or integer(
            candidate_query_spec.get("post_close_tail_s"),
            "candidate accounting tail",
        )
        != ACCOUNTING_TAIL_S
    ):
        raise EvidenceError(
            "current candidate evidence does not cover the exact accounting lifecycle"
        )
    raw_watermarks = _object(
        candidate_query.get("source_watermark_unix_s"), "candidate source watermarks"
    )
    required_watermarks = {"splits_merges", "trade_history"}
    if set(raw_watermarks) != required_watermarks or any(
        integer(raw_watermarks[name], f"{name} watermark") < accounting_end
        for name in required_watermarks
    ):
        raise EvidenceError("current candidate sources end before the accounting lifecycle")
    conditions = {str(row["condition_id"]).lower() for row in universe_rows}
    if (len(conditions) != integer(selection.get("required_conditions"),
                                   "required conditions")
            or len(conditions) != integer(expected_grid.get("windows_per_wallet"),
                                           "windows per wallet")):
        raise EvidenceError("cohort condition count differs from the selection rule")

    candidates, results = validated_attribution_artifact(
        candidate, attribution, candidate_sha, expected_revision
    )
    source: dict[tuple[object, ...], Mapping[str, object]] = {}
    for raw in candidates:
        row = _object(raw, "candidate row")
        key = _candidate_key(row)
        if key in source:
            raise EvidenceError("cohort candidate rows are duplicated")
        source[key] = row

    joined: set[tuple[object, ...]] = set()
    all_receipts: dict[str, str] = {}
    qualifying: dict[str, list[tuple[Mapping[str, object], Mapping[str, object], str]]] = {}
    for raw in results:
        result = _object(raw, "attribution result")
        row = _object(result.get("candidate"), "attributed candidate")
        key = _candidate_key(row)
        candidate_row = source.get(key)
        if candidate_row is None or key in joined:
            raise EvidenceError("attribution does not bijectively join the candidates")
        joined.add(key)
        proof = _object(result.get("attribution"), "attribution proof")
        wallet = str(proof.get("wallet") or "").lower()
        counterparty = str(proof.get("counterparty") or "").lower()
        receipt = digest(str(result.get("receipt_sha256") or ""), "receipt_sha256")
        tx_hash = str(candidate_row.get("tx_hash") or "").lower()
        prior_receipt = all_receipts.get(tx_hash)
        if prior_receipt is not None and prior_receipt != receipt:
            raise EvidenceError("one candidate transaction has conflicting receipt hashes")
        all_receipts[tx_hash] = receipt
        if (
            candidate_row.get("adapter_kind") == selection.get("adapter_kind")
            and candidate_row.get("candidate_scope") == selection.get("candidate_scope")
            and str(candidate_row.get("adapter") or "").lower() == selection.get("adapter")
            and candidate_row.get("op") == selection.get("op")
            and str(candidate_row.get("amount") or "") == selection.get("amount_base")
            and proof.get("classification") == selection.get("classification")
            and wallet == counterparty
            and selection_start
            <= integer(candidate_row.get("source_block_timestamp"), "source timestamp")
            < selection_end
        ):
            if not WALLET_RE.fullmatch(wallet):
                raise EvidenceError("qualifying cohort wallet is malformed")
            qualifying.setdefault(wallet, []).append((candidate_row, proof, receipt))
    if joined != set(source):
        raise EvidenceError("attribution omits one or more candidate rows")

    precursor = sorted(
        wallet for wallet, rows in qualifying.items()
        if len(rows) == len(conditions)
        and {str(row[0]["condition_id"]).lower() for row in rows} == conditions
    )
    if (len(precursor) != integer(selection.get("qualifying_precursor_wallet_count"),
                                  "precursor wallet count")
            or sha256(canonical(precursor)) != selection.get(
                "qualifying_precursor_wallets_sha256")):
        raise EvidenceError("qualifying precursor cohort changed")
    reference = str(selection.get("reference_wallet") or "").lower()
    wallets = sorted(wallet for wallet in precursor if wallet != reference)
    frozen_wallets = manifest.get("wallets")
    if (not isinstance(frozen_wallets, list) or wallets != frozen_wallets
            or sha256(canonical(wallets)) != projections.get("wallets_sha256")):
        raise EvidenceError("derived sibling wallets differ from the manifest")

    split_rows: list[dict[str, object]] = []
    split_transactions: set[str] = set()
    for wallet in wallets:
        for row, _, receipt in qualifying[wallet]:
            tx_hash = str(row["tx_hash"]).lower()
            tokens = row.get("token_ids")
            if not isinstance(tokens, list):
                raise EvidenceError("qualifying split lost its token list")
            if tx_hash in split_transactions:
                raise EvidenceError("cohort split receipt transaction is duplicated")
            split_transactions.add(tx_hash)
            split_rows.append({
                "wallet": wallet,
                "condition_id": str(row["condition_id"]).lower(),
                "tx_hash": tx_hash,
                "source_block_number": integer(row["source_block_number"], "source block"),
                "source_log_index": integer(row["source_log_index"], "source log"),
                "amount": str(row["amount"]),
                "token_ids": list(tokens),
                "receipt_sha256": receipt,
            })
    split_rows.sort(key=lambda row: (
        str(row["wallet"]), str(row["condition_id"]),
        integer(row["source_block_number"], "source block"),
        integer(row["source_log_index"], "source log"),
    ))
    wallet_conditions = {(str(row["wallet"]), str(row["condition_id"])) for row in split_rows}
    expected_rows = integer(expected_grid.get("wallet_windows"), "wallet-window count")
    if (len(wallets) != integer(expected_grid.get("wallets"), "wallet count")
            or len(split_rows) != expected_rows or len(wallet_conditions) != expected_rows
            or len(split_rows) != integer(expected_grid.get("qualifying_splits"),
                                          "qualifying splits")
            or sha256(canonical(split_rows)) != projections.get("split_evidence_sha256")):
        raise EvidenceError("cohort split evidence differs from the fixed 10x31 grid")
    return {
        "wallets": wallets,
        "outcome_free_universe": universe_rows,
        "split_evidence": split_rows,
        "selection_lifecycle": dict(selection_lifecycle),
        "accounting_lifecycle": {
            "lifecycle_start": selection_start,
            "lifecycle_end_exclusive": accounting_end,
            "post_close_tail_s": ACCOUNTING_TAIL_S,
            "source_watermark_unix_s": {
                name: integer(raw_watermarks[name], f"{name} watermark")
                for name in sorted(required_watermarks)
            },
        },
        "gate_spec": dict(gate_spec),
        "counts": {"wallets": len(wallets), "windows": len(conditions),
                   "wallet_windows": len(split_rows)},
    }, all_receipts


def _historical_sources(manifest: Mapping[str, object]) -> tuple[dict[str, str], dict[str, str]]:
    sources = _object(manifest.get("sources"), "manifest historical sources")
    if set(sources) != {"candidate", "attribution", "receipt_cache"}:
        raise EvidenceError("manifest historical source set is invalid")
    paths: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for name in sorted(sources):
        source = _object(sources[name], f"manifest historical source {name}")
        if set(source) != {"path", "sha256"}:
            raise EvidenceError("manifest historical source shape is invalid")
        raw_path = str(source.get("path") or "")
        path = Path(raw_path)
        if (
            not raw_path
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != raw_path
        ):
            raise EvidenceError("manifest historical source path is not canonical")
        paths[name] = raw_path
        hashes[name] = digest(str(source.get("sha256") or ""), f"historical {name} SHA-256")
    return paths, hashes


def _current_path(repo: Path, path: Path, label: str) -> tuple[Path, str]:
    root = repo.resolve()
    resolved = (path if path.is_absolute() else root / path).resolve()
    if not resolved.is_relative_to(root):
        raise EvidenceError(f"current {label} path escapes the repository")
    return resolved, resolved.relative_to(root).as_posix()


def verify(
    repo: Path,
    candidate_path: Path,
    candidate_sha: str,
    attribution_path: Path,
    attribution_sha: str,
    receipt_cache_path: Path,
    receipt_cache_sha: str,
    expected_revision: Mapping[str, object],
) -> dict[str, object]:
    expected = validated_revision_manifest(expected_revision)
    expected_sources = _object(expected.get("source_sha256"), "revision sources")
    manifest_path = (repo.resolve() / FROZEN_MANIFEST).resolve()
    if not manifest_path.is_relative_to(repo.resolve()):
        raise EvidenceError("frozen manifest path escapes the repository")
    manifest_sha = digest(
        str(expected_sources.get(FROZEN_MANIFEST.as_posix()) or ""),
        "frozen manifest revision SHA-256",
    )
    manifest = load_json(manifest_path, manifest_sha, SCHEMA)
    historical_paths, historical_hashes = _historical_sources(manifest)
    source_args = {
        "candidate": (candidate_path, candidate_sha),
        "attribution": (attribution_path, attribution_sha),
        "receipt_cache": (receipt_cache_path, receipt_cache_sha),
    }
    paths: dict[str, Path] = {}
    relative_paths: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for name, (path, raw_hash) in source_args.items():
        paths[name], relative_paths[name] = _current_path(repo, path, name)
        hashes[name] = digest(raw_hash, f"current {name} SHA-256")
    candidate = load_json(
        paths["candidate"], hashes["candidate"],
        "project-fail-adapter-receipt-candidates-v1",
    )
    attribution = load_json(
        paths["attribution"], hashes["attribution"],
        "project-fail-adapter-receipt-attribution-v1",
    )
    if attribution.get("input_sha256") != hashes["candidate"]:
        raise EvidenceError("cohort attribution does not bind its candidate source")
    proof, receipts = derive(
        candidate, attribution, manifest,
        hashes["candidate"], expected_revision,
    )
    verify_receipts(paths["receipt_cache"], hashes["receipt_cache"], receipts)
    proof.update({
        "schema": PROOF_SCHEMA,
        "selection_manifest_sha256": manifest_sha,
        "selection_source_paths": historical_paths,
        "selection_source_sha256": historical_hashes,
        "source_paths": relative_paths,
        "source_sha256": hashes,
    })
    return proof


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--attribution", type=Path, required=True)
    parser.add_argument("--attribution-sha256", required=True)
    parser.add_argument("--receipt-cache", type=Path, required=True)
    parser.add_argument("--receipt-cache-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    try:
        start_revision = revision(repo)
        proof = verify(
            repo,
            args.candidate,
            digest(args.candidate_sha256, "candidate SHA-256"),
            args.attribution,
            digest(args.attribution_sha256, "attribution SHA-256"),
            args.receipt_cache,
            digest(args.receipt_cache_sha256, "receipt cache SHA-256"),
            start_revision,
        )
        proof["revision"] = start_revision
        for name, raw_path in _object(proof["source_paths"], "proof source paths").items():
            expected_hash = str(_object(proof["source_sha256"], "proof source hashes")[name])
            if hash_file(repo / str(raw_path)) != expected_hash:
                raise EvidenceError(f"current {name} source changed during verification")
        if revision(repo) != start_revision:
            raise EvidenceError("cohort sources changed during verification")
        raw = canonical(proof) + b"\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as handle:
            handle.write(raw)
    except (EvidenceError, OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps({"output": str(args.output), "sha256": sha256(raw),
                      "wallets": proof["counts"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
