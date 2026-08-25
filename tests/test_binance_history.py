from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import pytest

from tools.binance_history import (
    ArchiveSpec,
    read_futures_seconds,
    read_spot_seconds,
)


def _zip(path: Path, rows: list[list[object]]) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("data.csv", buffer.getvalue())


def test_spot_microsecond_kline_is_preserved_as_one_second(tmp_path: Path) -> None:
    path = tmp_path / "spot.zip"
    _zip(path, [[
        1_787_000_000_000_000, "100", "102", "99", "101", "3",
        1_787_000_000_999_999, "303", 7, "2", "202", "0",
    ]])
    rows = list(read_spot_seconds([path]))
    assert rows[0].ts == 1_787_000_000
    assert rows[0].flow_imbalance == pytest.approx(1 / 3)
    assert rows[0].trades == 7


def test_futures_trades_aggregate_price_and_taker_flow_by_second(tmp_path: Path) -> None:
    path = tmp_path / "futures.zip"
    _zip(path, [
        [1, "100", "2", 1, 1, 1_787_000_000_100, "false"],
        [2, "101", "1", 2, 2, 1_787_000_000_900, "true"],
        [3, "99", "1", 3, 3, 1_787_000_001_100, "true"],
    ])
    rows = list(read_futures_seconds([path]))
    assert [(row.ts, row.open, row.high, row.low, row.close) for row in rows] == [
        (1_787_000_000, 100, 101, 100, 101),
        (1_787_000_001, 99, 99, 99, 99),
    ]
    assert rows[0].quote_volume == 301
    assert rows[0].taker_buy_quote == 200


def test_archive_paths_are_official_daily_products() -> None:
    import datetime as dt

    day = dt.date(2026, 8, 24)
    spot = ArchiveSpec("spot", "BTCUSDT", day)
    futures = ArchiveSpec("futures_um", "BTCUSDT", day)
    assert spot.url.endswith("/spot/daily/klines/BTCUSDT/1s/BTCUSDT-1s-2026-08-24.zip")
    assert futures.url.endswith(
        "/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-08-24.zip"
    )
