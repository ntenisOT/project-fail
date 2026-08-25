from __future__ import annotations

import sqlite3

import pytest

from paper.ledger import Ledger
from paper.report import snapshot_one, text, tg_text


def test_report_ranks_adverse_floor_and_charges_invalid_inventory(tmp_path) -> None:
    db_path = tmp_path / "paper.db"
    ledger = Ledger(str(db_path))
    for strategy, cash, shares, pnl in (
        ("neutral_trap", -3.0, 10.0, 2.0),
        ("safe", 0.1, 0.0, 0.1),
    ):
        ledger.record_settlement(2.0, strategy, "btc", f"slug-{strategy}", {
            "cash": cash, "residual": 0.0, "pnl": pnl, "capital": 3.0,
            "buys": 1, "sells": 0, "resid_shares": shares, "n_fills": 1,
            "outcome_up": True,
        })
        ledger.record_metrics(2.0, strategy, "btc", f"slug-{strategy}", {})
    ledger.record_invalid_window(3.0, "safe", "btc", "invalid-safe", {
        "reason": "ws_reconnect", "n_fills": 1, "capital": 0.5,
        "cash": -0.5, "up_shares": 5.0, "down_shares": 0.0,
        "event_lag_ms": 0.0,
    })
    ledger.close()

    with sqlite3.connect(db_path) as db:
        safe = snapshot_one(db, "safe")
    assert safe.invalid_floor == pytest.approx(-0.5)
    assert safe.worst_pnl == pytest.approx(-0.4)

    full, telegram = text(str(db_path)), tg_text(str(db_path))
    assert full.index("safe") < full.index("neutral_trap")
    assert telegram.index("safe") < telegram.index("neutral_trap")
    assert "floor" in telegram
