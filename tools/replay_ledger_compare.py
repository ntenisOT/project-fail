#!/usr/bin/env python3
"""Fail unless a replay JSON exactly reproduces the live paper cohort ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
from collections.abc import Sequence


class LedgerMismatch(RuntimeError):
    """Raised when replay records differ from the live SQLite ledger."""


def _sha256(path: str | pathlib.Path) -> str:
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_replay(
    path: str | pathlib.Path,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("replay JSON is not an object")
    records = payload.get("records")
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ValueError("replay JSON has no valid records list")
    if not records:
        raise LedgerMismatch("replay has no cohort records; parity would be vacuous")
    return payload, records


def _first_difference(
    name: str, expected: list[tuple[object, ...]], actual: list[tuple[object, ...]],
) -> None:
    if expected == actual:
        return
    limit = min(len(expected), len(actual))
    index = next((i for i in range(limit) if expected[i] != actual[i]), limit)
    expected_row = expected[index] if index < len(expected) else "<missing>"
    actual_row = actual[index] if index < len(actual) else "<missing>"
    raise LedgerMismatch(
        f"{name} mismatch at row {index}: expected={expected_row!r} "
        f"actual={actual_row!r}; counts={len(expected)}/{len(actual)}"
    )


def compare_replay_to_ledger(
    replay_json: str | pathlib.Path, ledger_path: str | pathlib.Path,
    dataset_path: str | pathlib.Path,
) -> dict[str, int]:
    """Compare every finalized cohort row after binding replay, capture, and ledger."""
    replay, records = _require_replay(replay_json)
    dataset_sha256 = _sha256(dataset_path)
    if replay.get("capture_dataset_sha256") != dataset_sha256:
        raise LedgerMismatch("replay is not bound to the supplied capture dataset")
    dataset = json.loads(pathlib.Path(dataset_path).read_text(encoding="utf-8"))
    if not isinstance(dataset, dict):
        raise LedgerMismatch("capture dataset is not an object")
    model_identity = dataset.get("model_identity")
    if not isinstance(model_identity, dict):
        raise LedgerMismatch("capture dataset lacks model identity")
    expected_metadata = {
        "capture_label": replay.get("capture_label"),
        "board_hash": replay.get("captured_board_hash"),
        "model_hash": replay.get("captured_model_hash"),
    }
    dataset_metadata = {
        "capture_label": dataset.get("label"),
        "board_hash": dataset.get("board_hash"),
        "model_hash": model_identity.get("sha256"),
    }
    if expected_metadata != dataset_metadata or any(
        not isinstance(value, str) or not value for value in expected_metadata.values()
    ):
        raise LedgerMismatch("replay identity does not match the capture dataset")
    expected_fills = [
        tuple(row[key] for key in (
            "ts", "strategy", "asset", "slug", "action", "price", "size",
            "signed_cash", "outcome_up",
        ))
        for row in records if "action" in row
    ]
    expected_settlements = [
        tuple(row[key] for key in (
            "ts", "strategy", "asset", "slug", "cash", "residual", "pnl",
            "capital", "buys", "sells", "resid_shares", "n_fills", "outcome_up",
        )) + (row["metrics"],)
        for row in records if "metrics" in row
    ]
    expected_invalid = [
        tuple(row[key] for key in (
            "ts", "strategy", "asset", "slug", "reason", "n_fills", "capital",
            "cash", "up_shares", "down_shares", "event_lag_ms",
        ))
        for row in records if "reason" in row
    ]
    if len(expected_fills) + len(expected_settlements) + len(expected_invalid) != len(records):
        raise ValueError("replay JSON contains an unknown cohort record shape")

    uri = pathlib.Path(ledger_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        metadata_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_metadata'"
        ).fetchone()
        if metadata_table is None:
            raise LedgerMismatch("live ledger has no run identity")
        try:
            actual_metadata = {
                str(key): json.loads(value)
                for key, value in db.execute("SELECT key,value FROM run_metadata")
            }
        except json.JSONDecodeError as exc:
            raise LedgerMismatch("live ledger run identity is malformed") from exc
        if actual_metadata != expected_metadata:
            raise LedgerMismatch(
                f"live ledger run identity differs: "
                f"{actual_metadata!r}/{expected_metadata!r}"
            )
        actual_fills = db.execute(
            """SELECT ts,strategy,asset,slug,action,price,size,signed_cash,outcome_up
                 FROM fills ORDER BY rowid"""
        ).fetchall()
        settlement_rows = db.execute(
            """SELECT ts,strategy,asset,slug,cash,residual,pnl,capital,buys,sells,
                      resid_shares,n_fills,outcome_up
                 FROM settlements ORDER BY rowid"""
        ).fetchall()
        metric_rows = db.execute(
            "SELECT ts,strategy,asset,slug,data FROM window_metrics ORDER BY rowid"
        ).fetchall()
        if len(settlement_rows) != len(metric_rows):
            raise LedgerMismatch(
                "live settlement/metric row counts differ: "
                f"{len(settlement_rows)}/{len(metric_rows)}"
            )
        actual_settlements = []
        for settlement, metric in zip(settlement_rows, metric_rows, strict=True):
            if settlement[:4] != metric[:4]:
                raise LedgerMismatch(
                    f"live settlement/metric identity differs: {settlement[:4]!r}/"
                    f"{metric[:4]!r}"
                )
            actual_settlements.append(settlement + (json.loads(metric[4]),))
        actual_invalid = db.execute(
            """SELECT ts,strategy,asset,slug,reason,n_fills,capital,cash,
                      up_shares,down_shares,event_lag_ms
                 FROM invalid_windows ORDER BY rowid"""
        ).fetchall()

    _first_difference("fills", expected_fills, actual_fills)
    _first_difference("settlements", expected_settlements, actual_settlements)
    _first_difference("invalid windows", expected_invalid, actual_invalid)
    return {
        "fills": len(expected_fills),
        "settlements": len(expected_settlements),
        "invalid_windows": len(expected_invalid),
        "records": len(records),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay_json")
    parser.add_argument("ledger")
    parser.add_argument("--dataset", required=True)
    args = parser.parse_args(argv)
    counts = compare_replay_to_ledger(args.replay_json, args.ledger, args.dataset)
    print("EXACT live/replay ledger parity | " + " | ".join(
        f"{name}={value}" for name, value in counts.items()
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
