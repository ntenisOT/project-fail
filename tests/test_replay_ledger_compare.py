from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from paper.ledger import Ledger
from tools.replay_ledger_compare import LedgerMismatch, compare_replay_to_ledger


def test_replay_ledger_comparison_is_exact_and_detects_mutation(tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    ledger = Ledger(str(db_path))
    ledger.record_run_metadata({
        "capture_label": "cohort", "board_hash": "board", "model_hash": "model",
    })
    ledger.record_fill(1.0, "probe", "btc", "slug", {
        "action": "BUY_UP", "price": 0.4, "size": 5.0,
        "signed_cash": -2.0, "outcome_up": 1,
    })
    ledger.record_settlement(2.0, "probe", "btc", "slug", {
        "cash": 3.0, "residual": 0.0, "pnl": 1.0, "capital": 2.0,
        "buys": 1, "sells": 0, "resid_shares": 0.0, "n_fills": 1,
        "outcome_up": True,
    })
    ledger.record_metrics(2.0, "probe", "btc", "slug", {"queue": 4})
    ledger.close()
    dataset = tmp_path / "cohort.dataset.json"
    dataset.write_text(json.dumps({
        "label": "cohort", "board_hash": "board",
        "model_identity": {"sha256": "model"},
    }))
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({
        "capture_label": "cohort",
        "capture_dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "captured_board_hash": "board", "captured_model_hash": "model",
        "records": [
        {"ts": 1.0, "strategy": "probe", "asset": "btc", "slug": "slug",
         "action": "BUY_UP", "price": 0.4, "size": 5.0,
         "signed_cash": -2.0, "outcome_up": 1},
        {"ts": 2.0, "strategy": "probe", "asset": "btc", "slug": "slug",
         "cash": 3.0, "residual": 0.0, "pnl": 1.0, "capital": 2.0,
         "buys": 1, "sells": 0, "resid_shares": 0.0, "n_fills": 1,
         "outcome_up": 1, "metrics": {"queue": 4}},
    ]}))

    assert compare_replay_to_ledger(replay, db_path, dataset)["records"] == 2

    # Mutate through SQLite only after proving the exact green path.
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE fills SET price=0.41")
    with pytest.raises(LedgerMismatch, match="fills mismatch"):
        compare_replay_to_ledger(replay, db_path, dataset)


def test_replay_ledger_comparison_rejects_vacuous_parity(tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    ledger = Ledger(str(db_path))
    ledger.close()
    dataset = tmp_path / "dataset.json"
    dataset.write_text("{}")
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps({"records": []}))

    with pytest.raises(LedgerMismatch, match="vacuous"):
        compare_replay_to_ledger(replay, db_path, dataset)
