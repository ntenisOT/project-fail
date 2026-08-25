#!/usr/bin/env python3
"""Descriptive whole-second winner-flow study using lagged Binance features.

Polygon block timestamps are integer-second settlement timestamps. Polymarket
orders are matched offchain before settlement, so this tool deliberately
aggregates fills by block and never interprets the result as a causal or
subsecond reaction study.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor

import clickhouse_connect  # type: ignore[import-untyped]

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.binance_history import (
    ArchiveSpec,
    SecondBar,
    download_archive,
)
from tools.binance_second_cache import cached_second_bars
from tools.market_windows import resolve_windows
from tools.top_setters import DEFAULT_CACHE, parse_timestamp
from tools.wallet_pairs import fetch_buy_fills
from tools.winner_signal_metrics import (
    CUTOFF_MARGINS_S,
    LOOKBACK_HORIZONS_S,
    MIN_INFERENCE_UTC_DAYS,
    MIN_INFERENCE_WALLETS,
    MIN_INFERENCE_WINDOWS,
    ROLES,
    SOURCES,
    BlockAction,
    aggregate_block_actions,
    summarize_association,
)
from tools.wallet_timing import WALLET_RE


ASSET = "btc"
SYMBOL = "BTCUSDT"
SCHEMA = "project-fail-winner-signal-study-v1"
CAVEAT = (
    "Exploratory interval-censored block-settlement-time association of buy-fill "
    "imbalance only. Polygon block timestamps are whole-second onchain settlement "
    "times after offchain matching. It does not identify order-placement reactions, "
    "net wallet exposure, lifecycle PnL, or a tradable threshold. The 36 role/source/"
    "margin/horizon specifications are correlated and the analyzed holdout was "
    "previously observable and is non-prospective; no strategy is validated."
)


@dataclasses.dataclass(frozen=True)
class FrozenStudy:
    start: int
    end: int
    discovery_end: int
    holdout_start: int
    wallets: tuple[str, ...]

    def __post_init__(self) -> None:
        boundaries = (
            self.start, self.end, self.discovery_end, self.holdout_start,
        )
        if any(boundary % 300 for boundary in boundaries):
            raise ValueError("frozen study timestamps must be five-minute aligned")


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_frozen_study(path: pathlib.Path) -> FrozenStudy:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("period"), dict):
        raise ValueError("lifecycle JSON has no period object")
    period = payload["period"]
    start, end = int(str(period["start"])), int(str(period["end"]))
    discovery_end = int(str(payload["discovery_end"]))
    holdout_start = int(str(payload["holdout_start"]))
    raw_wallets = payload.get("wallets")
    if not isinstance(raw_wallets, list):
        raise ValueError("lifecycle JSON has no frozen wallet list")
    wallets = tuple(sorted({str(wallet).lower() for wallet in raw_wallets}))
    if (end < start or not start <= discovery_end < holdout_start <= end
            or not wallets or any(not WALLET_RE.fullmatch(wallet) for wallet in wallets)):
        raise ValueError("invalid frozen period, split, or wallet list")
    return FrozenStudy(start, end, discovery_end, holdout_start, wallets)


def _archive_specs(start: int, end: int) -> list[ArchiveSpec]:
    first = dt.datetime.fromtimestamp(
        start - max(CUTOFF_MARGINS_S) - max(LOOKBACK_HORIZONS_S) - 1, dt.UTC
    ).date()
    # ``end`` is the inclusive start of the final five-minute market. Its
    # terminal observation is the final second of that window, not midnight of
    # the following UTC day.
    last = dt.datetime.fromtimestamp(end + 299, dt.UTC).date()
    dates = [first + dt.timedelta(days=offset)
             for offset in range((last - first).days + 1)]
    return [ArchiveSpec(market, SYMBOL, day)
            for day in dates for market in ("spot", "futures_um")]


def _verified_archives(
    start: int, end: int, directory: pathlib.Path, workers: int,
) -> tuple[dict[str, list[pathlib.Path]], list[dict[str, object]]]:
    specs = _archive_specs(start, end)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        paths = list(pool.map(lambda spec: download_archive(spec, directory), specs))
    by_source: dict[str, list[pathlib.Path]] = {source: [] for source in SOURCES}
    manifest: list[dict[str, object]] = []
    for spec, path in sorted(zip(specs, paths, strict=True), key=lambda row: (
        row[0].market, row[0].date,
    )):
        source = "spot" if spec.market == "spot" else "futures"
        by_source[source].append(path)
        manifest.append({
            "source": source,
            "date": spec.date.isoformat(),
            "file": path.name,
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
        })
    return by_source, manifest


def _bar_map(rows: Iterable[SecondBar]) -> dict[int, SecondBar]:
    result: dict[int, SecondBar] = {}
    for row in rows:
        previous = result.get(row.ts)
        if previous is not None and previous != row:
            raise ValueError(f"conflicting Binance bar at {row.ts}")
        result[row.ts] = row
    return result


def _split_counts(actions: Sequence[BlockAction], split: str) -> dict[str, object]:
    rows = [row for row in actions if row.split == split]
    return {
        "action_groups": len(rows),
        "wallets": len({row.wallet for row in rows}),
        "windows": len({row.slug for row in rows}),
        "utc_days": len({
            dt.datetime.fromtimestamp(row.window_start, dt.UTC).date().isoformat()
            for row in rows
        }),
        "maker_groups": sum(row.role == "maker" for row in rows),
        "taker_groups": sum(row.role == "taker" for row in rows),
    }


def build_report(
    study: FrozenStudy, actions: Sequence[BlockAction],
    bar_maps: Mapping[str, Mapping[int, SecondBar]], *,
    analysis_end: int,
    lifecycle_path: pathlib.Path, window_cache: pathlib.Path,
    archive_manifest: Sequence[Mapping[str, object]], fill_counts: Mapping[str, int],
) -> dict[str, object]:
    outside = [
        row for row in actions
        if not study.start <= row.window_start <= analysis_end
    ]
    if outside:
        raise ValueError(
            f"{len(outside)} actions fall outside the bounded analysis period"
        )
    results = [
        summarize_association(
            actions, bar_maps[source], split=split, role=role, source=source,
            margin_s=margin, horizon_s=horizon,
        )
        for split in ("discovery", "holdout")
        for role in ROLES
        for source in SOURCES
        for margin in CUTOFF_MARGINS_S
        for horizon in LOOKBACK_HORIZONS_S
    ]
    return {
        "schema": SCHEMA,
        "caveat": CAVEAT,
        "parameters": {
            "asset": ASSET,
            "symbol": SYMBOL,
            "event_window": "window_start <= block_ts < window_start + 300",
            "aggregation_key": [
                "split", "wallet", "slug", "block_number", "block_ts", "role",
            ],
            "cutoff_margins_s": list(CUTOFF_MARGINS_S),
            "lookback_horizons_s": list(LOOKBACK_HORIZONS_S),
            "feature_definition": (
                "10000*ln(close[block_ts-margin-1]/"
                "close[block_ts-margin-horizon-1]); complete bars only"
            ),
            "roles": list(ROLES),
            "sources": list(SOURCES),
            "economics": (
                "buy-fill terminal markouts only, not wallet PnL; explicit taker "
                "fees included; maker rebates, sells, splits, merges, and prior "
                "inventory excluded"
            ),
            "specification_grid": {
                "status": "exploratory",
                "cells_per_split": (
                    len(ROLES) * len(SOURCES) * len(CUTOFF_MARGINS_S)
                    * len(LOOKBACK_HORIZONS_S)
                ),
                "reported_splits": 2,
                "correlated": True,
                "selection_status": "no specification or threshold validated",
            },
            "inference_count_gates": {
                "wallets": MIN_INFERENCE_WALLETS,
                "windows": MIN_INFERENCE_WINDOWS,
                "utc_days": MIN_INFERENCE_UTC_DAYS,
            },
            "claim_level": "exploratory_descriptive_only",
            "strategy_validated": False,
            "holdout_evaluation": {
                "status": "previously_observable_non_prospective",
                "prospective": False,
                "confirmatory": False,
            },
            "analysis_period": {
                "start": study.start,
                "end": analysis_end,
                "end_is_inclusive_window_start": True,
            },
        },
        "frozen_cohort": {
            "period_start": study.start,
            "period_end": study.end,
            "discovery_end": study.discovery_end,
            "holdout_start": study.holdout_start,
            "wallets": list(study.wallets),
        },
        "dataset_files": {
            "lifecycle": {
                "file": lifecycle_path.as_posix(), "sha256": _sha256(lifecycle_path),
            },
            "window_cache": {
                "file": window_cache.as_posix(), "sha256": _sha256(window_cache),
            },
            "binance_archives": list(archive_manifest),
        },
        "data_quality": {
            **dict(fill_counts),
            "spot_second_bars": len(bar_maps["spot"]),
            "futures_second_bars": len(bar_maps["futures"]),
        },
        "split_counts": {
            split: _split_counts(actions, split)
            for split in ("discovery", "holdout")
        },
        "associations": results,
    }


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lifecycle-json", default="out/gen73-lifecycle-btc-7d.json"
    )
    parser.add_argument("--binance-dir", default="out/binance-history")
    parser.add_argument("--bar-cache-dir", default="out/binance-second-bars")
    parser.add_argument("--window-cache", default=DEFAULT_CACHE)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--analysis-end", type=parse_timestamp,
        help=(
            "inclusive final five-minute window start; keeps the frozen wallet "
            "cohort/split but permits an explicitly bounded archive period"
        ),
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--clickhouse-host", default="localhost")
    parser.add_argument("--clickhouse-port", type=int, default=8123)
    parser.add_argument("--clickhouse-user", default="copypoly")
    parser.add_argument("--clickhouse-database", default="copypoly")
    args = parser.parse_args(argv)
    if args.workers <= 0 or not 1 <= args.clickhouse_port <= 65535:
        parser.error("workers and ClickHouse port must be positive")
    return args


def bounded_analysis_end(study: FrozenStudy, requested: int | None) -> int:
    result = study.end if requested is None else requested
    if result < study.holdout_start:
        raise ValueError("analysis end must include at least one holdout window")
    if result > study.end:
        raise ValueError("analysis end exceeds the frozen lifecycle period")
    if result % 300:
        raise ValueError("analysis end must be five-minute aligned")
    return result


def _write_json(path: pathlib.Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    lifecycle_path = pathlib.Path(args.lifecycle_json)
    window_cache = pathlib.Path(args.window_cache)
    study = load_frozen_study(lifecycle_path)
    analysis_end = bounded_analysis_end(study, args.analysis_end)
    windows, missing = resolve_windows(
        [ASSET], study.start, analysis_end, window_cache,
        fetch_missing=False, allow_missing=False,
    )
    if missing or len(windows) != (analysis_end - study.start) // 300 + 1:
        raise RuntimeError("bounded BTC analysis window cohort is incomplete")
    by_slug = {window.slug: window for window in windows}
    archive_paths, archive_manifest = _verified_archives(
        study.start, analysis_end, pathlib.Path(args.binance_dir), args.workers,
    )
    bars = {
        source: _bar_map(cached_second_bars(
            archive_paths[source], source, pathlib.Path(args.bar_cache_dir),
        ))
        for source in SOURCES
    }
    client = clickhouse_connect.get_client(
        host=args.clickhouse_host, port=args.clickhouse_port,
        username=args.clickhouse_user,
        password=os.environ.get("CLICKHOUSE_PASSWORD", "copypoly"),
        database=args.clickhouse_database,
    )
    fills = fetch_buy_fills(client, windows, study.wallets)
    actions, fill_counts = aggregate_block_actions(
        fills, by_slug, study.holdout_start,
    )
    report = build_report(
        study, actions, bars, analysis_end=analysis_end, lifecycle_path=lifecycle_path,
        window_cache=window_cache, archive_manifest=archive_manifest,
        fill_counts={
            "resolved_windows": len(windows),
            "selected_wallets": len(study.wallets),
            **fill_counts,
        },
    )
    _write_json(pathlib.Path(args.output), report)
    print(json.dumps({
        "output": args.output,
        "wallets": len(study.wallets),
        "windows": len(windows),
        "analysis_end": analysis_end,
        "action_groups": len(actions),
        "claim_level": "exploratory_descriptive_only",
        "caveat": CAVEAT,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
