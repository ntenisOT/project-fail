"""Verified binary cache for normalized Binance one-second bars."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import struct
from collections.abc import Iterable, Iterator

from tools.binance_history import SecondBar, read_futures_seconds, read_spot_seconds


SCHEMA = "project-fail-binance-second-bars-v1"
MAGIC = b"PFBARS1\n"
BAR = struct.Struct("!q6dI")


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _paths(
    archive: pathlib.Path, directory: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    stem = directory / f"{archive.name}.bars-v1"
    return stem.with_suffix(stem.suffix + ".bin"), stem.with_suffix(
        stem.suffix + ".meta.json"
    )


def _read_cache(path: pathlib.Path, records: int) -> Iterator[SecondBar]:
    if records < 0:
        raise ValueError("negative cached bar count")
    expected = len(MAGIC) + records * BAR.size
    if path.stat().st_size != expected:
        raise ValueError("cached bar byte count mismatch")
    with path.open("rb") as handle:
        if handle.read(len(MAGIC)) != MAGIC:
            raise ValueError("cached bar magic mismatch")
        for _ in range(records):
            values = BAR.unpack(handle.read(BAR.size))
            yield SecondBar(*values)
        if handle.read(1):
            raise ValueError("cached bar trailing bytes")


def _valid_meta(
    meta_path: pathlib.Path, bars_path: pathlib.Path, *, market: str,
    archive: pathlib.Path, source_sha256: str,
) -> tuple[dict[str, object], bool]:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        valid = (
            isinstance(meta, dict)
            and meta.get("schema") == SCHEMA
            and meta.get("market") == market
            and meta.get("source") == archive.name
            and meta.get("source_sha256") == source_sha256
            and bars_path.is_file()
            and meta.get("bars_sha256") == _sha256(bars_path)
            and int(str(meta.get("records"))) >= 0
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}, False
    return meta, valid


def _build(
    archive: pathlib.Path, market: str, bars_path: pathlib.Path,
    meta_path: pathlib.Path, source_sha256: str,
) -> dict[str, object]:
    if market not in {"spot", "futures"}:
        raise ValueError(f"unsupported bar cache market: {market}")
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = bars_path.with_suffix(bars_path.suffix + ".tmp")
    reader = read_spot_seconds if market == "spot" else read_futures_seconds
    records = 0
    with temporary.open("wb") as handle:
        handle.write(MAGIC)
        for row in reader([archive]):
            handle.write(BAR.pack(
                row.ts, row.open, row.high, row.low, row.close,
                row.quote_volume, row.taker_buy_quote, row.trades,
            ))
            records += 1
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, bars_path)
    meta: dict[str, object] = {
        "schema": SCHEMA,
        "market": market,
        "source": archive.name,
        "source_sha256": source_sha256,
        "bars": bars_path.name,
        "bars_sha256": _sha256(bars_path),
        "records": records,
    }
    meta_temporary = meta_path.with_suffix(meta_path.suffix + ".tmp")
    meta_temporary.write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(meta_temporary, meta_path)
    return meta


def cached_second_bars(
    archives: Iterable[pathlib.Path], market: str, directory: pathlib.Path,
) -> Iterator[SecondBar]:
    """Yield normalized bars, rebuilding any stale derived cache atomically."""
    for archive in sorted(archives):
        source_sha256 = _sha256(archive)
        bars_path, meta_path = _paths(archive, directory)
        meta, valid = _valid_meta(
            meta_path, bars_path, market=market, archive=archive,
            source_sha256=source_sha256,
        )
        if not valid:
            meta = _build(
                archive, market, bars_path, meta_path, source_sha256,
            )
        yield from _read_cache(bars_path, int(str(meta["records"])))
