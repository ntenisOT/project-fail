from __future__ import annotations

import pytest

from tools.adapter_receipt_candidates import (
    DEFAULT_POST_CLOSE_TAIL_S,
    NEG_RISK_ADAPTER,
    OLD_FACTORY,
    STANDARD_ADAPTER,
    ExportError,
    _candidate_rows,
    _candidate_sql,
    _lifecycle_bounds,
    _validate_windows,
)
from tools.adapter_receipt_core import Candidate
from tools.market_windows import ResolvedWindow


def _window(start: int, suffix: int) -> ResolvedWindow:
    return ResolvedWindow(
        slug=f"btc-updown-5m-{start}", asset="btc", start=start,
        condition_id="0x" + f"{suffix:064x}", up_token=str(100 + suffix),
        down_token=str(200 + suffix), winner_up=suffix % 2,
    )


def test_candidates_label_adapters_preserve_units_and_deduplicate_identity() -> None:
    windows = [_window(300, 1)]
    common = (10, 4, 350, "0x" + "ab" * 32, windows[0].condition_id, "split")
    rows = [
        (*common, OLD_FACTORY, "1000001", 0),
        (*common, OLD_FACTORY, "1000001", 0),
        (11, 7, 351, "0x" + "bc" * 32, common[4], "merge", STANDARD_ADAPTER, "9", 1),
        (12, 8, 352, "0x" + "cd" * 32, common[4], "split", NEG_RISK_ADAPTER, "11", 0),
    ]

    candidates, duplicates = _candidate_rows(rows, windows, 10)

    assert duplicates == 1
    assert [row["adapter_kind"] for row in candidates] == [
        "legacy_clob_factory", "standard", "neg_risk",
    ]
    assert candidates[0]["amount"] == "1000001"
    assert candidates[0]["source_log_index"] == 4
    assert candidates[0]["token_ids"] == ["101", "201"]
    assert Candidate.from_mapping(candidates[0]).amount == 1_000_001
    with pytest.raises(ExportError, match="escaped"):
        _candidate_rows([(*common, OLD_FACTORY, "1", 1)], windows, 10)
    with pytest.raises(ExportError, match="malformed"):
        _candidate_rows([(*common, STANDARD_ADAPTER, "1", 2)], windows, 10)
    conflicting_log = (10, 5, *common[2:], OLD_FACTORY, "1000001", 0)
    with pytest.raises(ExportError, match="distinct source logs"):
        _candidate_rows([rows[0], conflicting_log], windows, 10)


def test_window_mapping_fails_closed_on_gap_or_duplicate_token() -> None:
    first, second = _window(300, 1), _window(600, 2)
    _validate_windows([first, second], 300, 600)

    with pytest.raises(ExportError, match="exact interval"):
        _validate_windows([first], 300, 600)
    duplicate = ResolvedWindow(
        slug="btc-updown-5m-600", asset="btc", start=600,
        condition_id=second.condition_id, up_token=first.up_token,
        down_token="999", winner_up=0,
    )
    with pytest.raises(ExportError, match="duplicate condition or token"):
        _validate_windows([first, duplicate], 300, 600)


def test_lifecycle_tail_is_measured_from_the_last_market_close() -> None:
    t0, t1 = _lifecycle_bounds(1_000_200, 1_009_200, 86_400)

    assert t0 < 1_000_200
    assert t1 == 1_009_200 + 300 + 86_400
    assert DEFAULT_POST_CLOSE_TAIL_S == 86_400


def test_legacy_clob_join_sums_split_and_merge_legs_separately() -> None:
    sql = _candidate_sql(100, 200)

    assert "FROM trade_history AS th FINAL" in sql
    assert "FROM splits_merges AS sm FINAL" in sql
    assert "if(th.maker_asset_id='0','split','merge') AS clob_op" in sql
    assert "GROUP BY clob_tx, clob_condition, clob_op, clob_token" in sql
    assert "sum(clob_delta)" not in sql
