#!/usr/bin/env python3
"""Verify the frozen, outcome-independent mint sibling cohort."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from tools.mint_attribution_validation import validated_attribution_artifact
from tools.mint_accounting_inputs import (
    EvidenceError,
    WALLET_RE,
    canonical,
    digest,
    integer,
    load_json,
    mapping,
    revision,
    sha256,
    verify_receipts,
)


SCHEMA = "project-fail-mint-sibling-cohort-v1"
PROOF_SCHEMA = "project-fail-mint-sibling-cohort-proof-v1"
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
    expected_grid = _object(manifest.get("expected_grid"), "manifest expected_grid")
    projections = _object(manifest.get("projections"), "manifest projections")
    universe_rows = _universe(candidate)
    candidate_query = _object(candidate.get("query"), "candidate query")
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
        if (
            candidate_row.get("adapter_kind") == selection.get("adapter_kind")
            and candidate_row.get("candidate_scope") == selection.get("candidate_scope")
            and str(candidate_row.get("adapter") or "").lower() == selection.get("adapter")
            and candidate_row.get("op") == selection.get("op")
            and str(candidate_row.get("amount") or "") == selection.get("amount_base")
            and proof.get("classification") == selection.get("classification")
            and wallet == counterparty
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

    split_rows, receipts = [], {}
    for wallet in wallets:
        for row, _, receipt in qualifying[wallet]:
            tx_hash = str(row["tx_hash"]).lower()
            tokens = row.get("token_ids")
            if not isinstance(tokens, list):
                raise EvidenceError("qualifying split lost its token list")
            if tx_hash in receipts:
                raise EvidenceError("cohort split receipt transaction is duplicated")
            receipts[tx_hash] = receipt
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
        "counts": {"wallets": len(wallets), "windows": len(conditions),
                   "wallet_windows": len(split_rows)},
    }, receipts


def verify(
    manifest_path: Path, manifest_sha: str, repo: Path,
    expected_revision: Mapping[str, object],
) -> dict[str, object]:
    manifest = load_json(manifest_path, manifest_sha, SCHEMA)
    sources = _object(manifest.get("sources"), "manifest sources")
    loaded: dict[str, Any] = {}
    paths: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for name in ("candidate", "attribution", "receipt_cache"):
        source = _object(sources.get(name), f"manifest source {name}")
        relative = Path(str(source.get("path") or ""))
        path = (repo / relative).resolve()
        if not path.is_relative_to(repo.resolve()):
            raise EvidenceError("cohort source path escapes the repository")
        paths[name] = path
        hashes[name] = digest(str(source.get("sha256") or ""), f"{name} SHA-256")
    loaded["candidate"] = load_json(
        paths["candidate"], hashes["candidate"],
        "project-fail-adapter-receipt-candidates-v1",
    )
    loaded["attribution"] = load_json(
        paths["attribution"], hashes["attribution"],
        "project-fail-adapter-receipt-attribution-v1",
    )
    if loaded["attribution"].get("input_sha256") != hashes["candidate"]:
        raise EvidenceError("cohort attribution does not bind its candidate source")
    proof, receipts = derive(
        loaded["candidate"], loaded["attribution"], manifest,
        hashes["candidate"], expected_revision,
    )
    verify_receipts(paths["receipt_cache"], hashes["receipt_cache"], receipts)
    proof.update({"schema": PROOF_SCHEMA, "manifest_sha256": manifest_sha,
                  "source_sha256": hashes})
    return proof


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    try:
        start_revision = revision(repo)
        proof = verify(
            args.manifest, digest(args.manifest_sha256, "manifest SHA-256"), repo,
            start_revision,
        )
        proof["revision"] = start_revision
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
