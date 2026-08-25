#!/usr/bin/env python3
"""Download verified Binance archives and normalize them to one-second bars."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import hashlib
import io
import os
import pathlib
import shutil
import sys
import urllib.request
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
ARCHIVE = "https://data.binance.vision/data"


@dataclasses.dataclass(frozen=True)
class SecondBar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float
    taker_buy_quote: float
    trades: int

    @property
    def flow_imbalance(self) -> float:
        if self.quote_volume <= 0:
            return 0.0
        return 2 * self.taker_buy_quote / self.quote_volume - 1


@dataclasses.dataclass(frozen=True)
class ArchiveSpec:
    market: str
    symbol: str
    date: dt.date

    @property
    def filename(self) -> str:
        day = self.date.isoformat()
        if self.market == "spot":
            return f"{self.symbol}-1s-{day}.zip"
        if self.market == "futures_um":
            return f"{self.symbol}-aggTrades-{day}.zip"
        raise ValueError(f"unsupported Binance archive market: {self.market}")

    @property
    def url(self) -> str:
        if self.market == "spot":
            folder = f"spot/daily/klines/{self.symbol}/1s"
        elif self.market == "futures_um":
            folder = f"futures/um/daily/aggTrades/{self.symbol}"
        else:
            raise ValueError(f"unsupported Binance archive market: {self.market}")
        return f"{ARCHIVE}/{folder}/{self.filename}"


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_checksum(spec: ArchiveSpec) -> str:
    request = urllib.request.Request(
        spec.url + ".CHECKSUM", headers={"User-Agent": "project-fail-forensics/1"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        value = response.read().decode("ascii").strip().split()[0].lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"invalid checksum for {spec.url}")
    return value


def download_archive(spec: ArchiveSpec, directory: pathlib.Path) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / spec.filename
    expected = _expected_checksum(spec)
    if target.exists() and _sha256(target) == expected:
        return target
    partial = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(
        spec.url, headers={"User-Agent": "project-fail-forensics/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
        actual = _sha256(partial)
        if actual != expected:
            raise ValueError(f"checksum mismatch for {spec.filename}: {actual} != {expected}")
        os.replace(partial, target)
    finally:
        if partial.exists():
            partial.unlink()
    return target


def timestamp_seconds(value: str) -> int:
    raw = int(value)
    if raw >= 100_000_000_000_000:
        return raw // 1_000_000
    if raw >= 100_000_000_000:
        return raw // 1_000
    raise ValueError(f"unexpected Binance timestamp: {raw}")


def _csv_rows(path: pathlib.Path) -> Iterator[list[str]]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in {path}, got {names}")
        with archive.open(names[0]) as raw:
            yield from csv.reader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))


def read_spot_seconds(paths: Iterable[pathlib.Path]) -> Iterator[SecondBar]:
    for path in sorted(paths):
        for row in _csv_rows(path):
            if len(row) < 11 or not row[0].isdigit():
                continue
            yield SecondBar(
                timestamp_seconds(row[0]), float(row[1]), float(row[2]),
                float(row[3]), float(row[4]), float(row[7]),
                float(row[10]), int(row[8]),
            )


def read_futures_seconds(paths: Iterable[pathlib.Path]) -> Iterator[SecondBar]:
    current_ts: int | None = None
    opening = high = low = close = quote = taker_buy = 0.0
    trades = 0
    for path in sorted(paths):
        for row in _csv_rows(path):
            if len(row) < 7 or not row[0].isdigit():
                continue
            ts = timestamp_seconds(row[5])
            price, quantity = float(row[1]), float(row[2])
            value = price * quantity
            if current_ts is not None and ts != current_ts:
                yield SecondBar(
                    current_ts, opening, high, low, close, quote, taker_buy, trades,
                )
                opening = high = low = close = quote = taker_buy = 0.0
                trades = 0
            if current_ts != ts:
                current_ts = ts
                opening = high = low = close = price
            else:
                high, low, close = max(high, price), min(low, price), price
            quote += value
            if row[6].strip().lower() == "false":
                taker_buy += value
            trades += 1
    if current_ts is not None:
        yield SecondBar(current_ts, opening, high, low, close, quote, taker_buy, trades)


def _dates(start: dt.date, end: dt.date) -> list[dt.date]:
    if end < start:
        raise ValueError("end date precedes start date")
    return [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--assets", default="btc")
    parser.add_argument("--markets", default="spot,futures_um")
    parser.add_argument("--out", default="out/binance-history")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    assets = [value.strip().lower() for value in args.assets.split(",") if value.strip()]
    markets = [value.strip() for value in args.markets.split(",") if value.strip()]
    if (not assets or set(assets) - set(SYMBOLS) or not markets
            or set(markets) - {"spot", "futures_um"} or args.workers <= 0
            or args.end < args.start):
        parser.error("invalid period, assets, markets, or worker count")
    args.assets, args.markets = assets, markets
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    directory = pathlib.Path(args.out)
    specs = [
        ArchiveSpec(market, SYMBOLS[asset], day)
        for day in _dates(args.start, args.end)
        for asset in args.assets for market in args.markets
    ]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        paths = list(pool.map(lambda spec: download_archive(spec, directory), specs))
    for path in sorted(paths):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
