from __future__ import annotations

import pathlib
import sqlite3
from typing import cast

import pytest

from paper.ledger import Ledger
from tools.pair_completion_economics import CompletionDataConflict, analyze_databases


def _settle(ledger: Ledger, slug: str, pnl: float, outcome_up: bool) -> None:
    ledger.record_settlement(600.0, "probe", "btc", slug, {
        "cash": pnl, "residual": 0.0, "pnl": pnl, "capital": 5.0,
        "buys": 1, "sells": 0, "resid_shares": 0.0, "n_fills": 1,
        "outcome_up": outcome_up,
    })


def test_completion_economics_reconstructs_pair_surplus_and_residue(tmp_path) -> None:
    path = pathlib.Path(tmp_path) / "paper.db"
    ledger = Ledger(str(path))
    complete = "btc-updown-5m-100"
    for ts, side, price, size in (
        (101.0, True, 0.60, 5.0),
        (103.0, False, 0.35, 3.0),
        (105.0, False, 0.30, 2.0),
    ):
        taker_fee = 0.02 if ts == 105.0 else 0.0
        ledger.record_fill(ts, "probe", "btc", complete, {
            "action": "taker_buy" if taker_fee else "buy",
            "price": price, "size": size,
            "signed_cash": -price * size - taker_fee, "outcome_up": side,
        })
    _settle(ledger, complete, 0.33, True)
    incomplete = "btc-updown-5m-400"
    ledger.record_fill(401.0, "probe", "btc", incomplete, {
        "action": "buy", "price": 0.70, "size": 5.0,
        "signed_cash": -3.5, "outcome_up": True,
    })
    _settle(ledger, incomplete, 1.5, True)
    ledger.close()

    result = analyze_databases([path], "probe")
    aggregate = cast(dict[str, object], result["aggregate"])

    assert aggregate["completion_rate"] == pytest.approx(0.5)
    assert aggregate["pair_surplus_usd"] == pytest.approx(0.33)
    assert aggregate["adverse_floor_usd"] == pytest.approx(-3.17)
    assert aggregate["completion_delay_p50_s"] == pytest.approx(2.0)
    assert aggregate["completion_delay_p90_s"] == pytest.approx(4.0)
    assert aggregate["zero_adverse_floor_completion_rate"] == pytest.approx(
        0.7 / 0.766
    )
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE settlements SET pnl=pnl+1 WHERE strategy=? AND slug=?",
            ("probe", complete),
        )
    with pytest.raises(CompletionDataConflict, match="settlement mismatch"):
        analyze_databases([path], "probe")


def test_completion_economics_keeps_unfinished_sources_separate(tmp_path) -> None:
    slug = "btc-updown-5m-100"
    unfinished_path = pathlib.Path(tmp_path) / "unfinished.db"
    unfinished = Ledger(str(unfinished_path))
    unfinished.record_fill(101.0, "probe", "btc", slug, {
        "action": "buy", "price": 0.4, "size": 5.0,
        "signed_cash": -2.0, "outcome_up": True,
    })
    unfinished.close()

    invalid_path = pathlib.Path(tmp_path) / "invalid.db"
    invalid = Ledger(str(invalid_path))
    invalid.record_invalid_window(102.0, "probe", "btc", slug, {
        "reason": "startup", "n_fills": 0, "capital": 0.0, "cash": 0.0,
        "up_shares": 0.0, "down_shares": 0.0, "event_lag_ms": 0.0,
    })
    invalid.close()

    result = analyze_databases([unfinished_path, invalid_path], "probe")
    assert cast(dict[str, object], result["selection"]) == {
        "finalized_windows": 1,
        "settled_windows": 0,
        "invalid_windows": 1,
        "duplicate_finalized_rows_collapsed": 0,
        "unfinished_source_rows_excluded": 1,
        "unfinished_slugs_without_finalization": 0,
    }

    settled_path = pathlib.Path(tmp_path) / "settled.db"
    settled = Ledger(str(settled_path))
    _settle(settled, slug, 0.0, True)
    settled.close()
    with pytest.raises(CompletionDataConflict):
        analyze_databases([invalid_path, settled_path], "probe")

    duplicate = Ledger(str(invalid_path))
    duplicate.record_invalid_window(103.0, "probe", "btc", slug, {
        "reason": "duplicate", "n_fills": 0, "capital": 0.0, "cash": 0.0,
        "up_shares": 0.0, "down_shares": 0.0, "event_lag_ms": 0.0,
    })
    duplicate.close()
    with pytest.raises(CompletionDataConflict, match="duplicate invalid_windows"):
        analyze_databases([invalid_path], "probe")
