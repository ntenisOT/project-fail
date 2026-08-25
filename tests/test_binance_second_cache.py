from __future__ import annotations

import csv
import io
import json
import zipfile

from tools.binance_second_cache import cached_second_bars


def _zip(path, rows: list[list[object]]) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("data.csv", buffer.getvalue())


def test_second_cache_round_trips_and_reuses_verified_binary(tmp_path) -> None:
    archive = tmp_path / "BTCUSDT-1s-2026-08-24.zip"
    _zip(archive, [[
        1_787_000_000_000_000, "100", "102", "99", "101", "3",
        1_787_000_000_999_999, "303", 7, "2", "202", "0",
    ]])
    directory = tmp_path / "cache"

    first = list(cached_second_bars([archive], "spot", directory))
    metadata = next(directory.glob("*.meta.json"))
    before = metadata.stat().st_mtime_ns
    second = list(cached_second_bars([archive], "spot", directory))

    assert first == second and first[0].ts == 1_787_000_000
    assert metadata.stat().st_mtime_ns == before


def test_second_cache_rebuilds_when_source_archive_changes(tmp_path) -> None:
    archive = tmp_path / "BTCUSDT-1s-2026-08-24.zip"
    directory = tmp_path / "cache"
    base = [
        1_787_000_000_000_000, "100", "100", "100", "100", "1",
        1_787_000_000_999_999, "100", 1, "1", "100", "0",
    ]
    _zip(archive, [base])
    assert list(cached_second_bars([archive], "spot", directory))[0].close == 100

    changed = list(base)
    changed[4] = "101"
    _zip(archive, [changed])
    rows = list(cached_second_bars([archive], "spot", directory))
    meta = json.loads(next(directory.glob("*.meta.json")).read_text())

    assert rows[0].close == 101
    assert meta["records"] == 1
