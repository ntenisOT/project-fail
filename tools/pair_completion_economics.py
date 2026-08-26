#!/usr/bin/env python3
"""Reconstruct terminal pair completion and residue economics from paper ledgers."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import pathlib
import sqlite3
import sys
from collections import defaultdict, deque
from collections.abc import Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from paper.pair_lots import weighted_quantile


class CompletionDataConflict(RuntimeError):
    """Raised when two source ledgers disagree about one finalized window."""


@dataclasses.dataclass(frozen=True)
class Fill:
    ts: float
    side_up: bool
    price: float
    unit_cost: float
    shares: float


@dataclasses.dataclass(frozen=True)
class Tape:
    source: str
    slug: str
    status: str
    status_at: float | None
    winner_up: bool | None
    settlement_pnl: float | None
    fills: tuple[Fill, ...]


@dataclasses.dataclass
class OpenLot:
    ts: float
    side_up: bool
    unit_cost: float
    shares: float


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _one_source(path: pathlib.Path, strategy: str) -> list[Tape]:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as db:
        fills: dict[str, list[Fill]] = defaultdict(list)
        for ts, slug, action, price, size, signed_cash, side_up in db.execute(
            """SELECT ts,slug,action,price,size,signed_cash,outcome_up FROM fills
               WHERE strategy=? ORDER BY ts,rowid""",
            (strategy,),
        ):
            if str(action).lower() not in ("buy", "taker_buy"):
                raise CompletionDataConflict(
                    f"{path}: {strategy} contains unsupported {action!r} fill"
                )
            if side_up not in (0, 1):
                raise CompletionDataConflict(
                    f"{path}: {strategy}/{slug} has invalid outcome side {side_up!r}"
                )
            numeric = (float(ts), float(price), float(size), float(signed_cash))
            if (not all(math.isfinite(value) for value in numeric)
                    or not 0 <= numeric[1] <= 1 or numeric[2] <= 0
                    or numeric[3] > 1e-9):
                raise CompletionDataConflict(
                    f"{path}: {strategy}/{slug} has invalid fill values {numeric!r}"
                )
            unit_cost = -numeric[3] / numeric[2]
            if unit_cost + 1e-8 < numeric[1]:
                raise CompletionDataConflict(
                    f"{path}: {strategy}/{slug} buy cash omits acquisition cost"
                )
            fills[str(slug)].append(
                Fill(numeric[0], bool(side_up), numeric[1], unit_cost, numeric[2])
            )
        for table in ("settlements", "invalid_windows"):
            if table == "invalid_windows" and not _has_table(db, table):
                continue
            duplicate = db.execute(
                f"""SELECT slug,count(*) FROM {table}
                    WHERE strategy=? GROUP BY slug HAVING count(*) > 1 LIMIT 1""",
                (strategy,),
            ).fetchone()
            if duplicate is not None:
                raise CompletionDataConflict(
                    f"{path}: duplicate {table} rows for {duplicate[0]}"
                )
        settlements = {
            str(slug): (float(ts), bool(winner), float(pnl))
            for slug, ts, winner, pnl in db.execute(
                """SELECT slug,ts,outcome_up,pnl FROM settlements
                   WHERE strategy=?""",
                (strategy,),
            )
        }
        invalid = (
            {str(slug): float(ts) for slug, ts in db.execute(
                "SELECT slug,ts FROM invalid_windows WHERE strategy=?", (strategy,),
            )}
            if _has_table(db, "invalid_windows") else {}
        )
        overlap = settlements.keys() & invalid.keys()
        if overlap:
            raise CompletionDataConflict(
                f"{path}: both settled and invalid status for {sorted(overlap)[0]}"
            )
        resolved = (
            {str(slug): bool(winner) for slug, winner in db.execute(
                "SELECT slug,outcome_up FROM resolved_windows"
            )}
            if _has_table(db, "resolved_windows") else {}
        )
    slugs = sorted(fills.keys() | settlements.keys() | invalid.keys())
    result: list[Tape] = []
    for slug in slugs:
        status_at: float | None
        winner: bool | None
        pnl: float | None
        if slug in settlements:
            status_at, winner, pnl = settlements[slug]
            status = "settled"
        elif slug in invalid:
            status_at, winner, pnl = invalid[slug], resolved.get(slug), None
            status = "invalid"
        else:
            status_at, winner, pnl = None, resolved.get(slug), None
            status = "unfinished"
        result.append(Tape(
            str(path), slug, status, status_at, winner, pnl,
            tuple(fills.get(slug, ())),
        ))
    return result


def _fingerprint(tape: Tape) -> tuple[object, ...]:
    return (
        tape.status, tape.winner_up, tape.settlement_pnl,
        tuple(dataclasses.astuple(fill) for fill in tape.fills),
    )


def _select_tapes(tapes: Sequence[Tape]) -> tuple[list[Tape], int, int, int]:
    grouped: dict[str, list[Tape]] = defaultdict(list)
    for tape in tapes:
        grouped[tape.slug].append(tape)
    selected: list[Tape] = []
    duplicates = unfinished_rows = unfinished_slugs = 0
    for slug, rows in sorted(grouped.items()):
        finalized = [row for row in rows if row.status != "unfinished"]
        unfinished_rows += len(rows) - len(finalized)
        if not finalized:
            unfinished_slugs += 1
            continue
        fingerprints = {_fingerprint(row) for row in finalized}
        if len(fingerprints) != 1:
            raise CompletionDataConflict(
                f"finalized ledgers disagree for {slug}: "
                f"{[row.source for row in finalized]}"
            )
        selected.append(sorted(finalized, key=lambda row: row.source)[0])
        duplicates += len(finalized) - 1
    return selected, duplicates, unfinished_rows, unfinished_slugs


def _window(tape: Tape) -> tuple[dict[str, object], list[tuple[float, float]]]:
    open_lots: dict[bool, deque[OpenLot]] = {True: deque(), False: deque()}
    opened = completed = paired_surplus = 0.0
    delays: list[tuple[float, float]] = []
    for fill in tape.fills:
        remaining = fill.shares
        opposite = open_lots[not fill.side_up]
        while remaining > 1e-9 and opposite:
            lot = opposite[0]
            matched = min(remaining, lot.shares)
            completed += matched
            paired_surplus += matched * (1 - lot.unit_cost - fill.unit_cost)
            delays.append((max(0.0, fill.ts - lot.ts), matched))
            remaining -= matched
            lot.shares -= matched
            if lot.shares <= 1e-9:
                opposite.popleft()
        if remaining > 1e-9:
            open_lots[fill.side_up].append(
                OpenLot(fill.ts, fill.side_up, fill.unit_cost, remaining)
            )
            opened += remaining
    residue = [lot for lots in open_lots.values() for lot in lots]
    incomplete = sum(lot.shares for lot in residue)
    incomplete_cost = sum(lot.shares * lot.unit_cost for lot in residue)
    unmatched_actual = None
    if tape.winner_up is not None:
        unmatched_actual = sum(
            lot.shares * (float(lot.side_up == tape.winner_up) - lot.unit_cost)
            for lot in residue
        )
    mechanism_actual = (
        None if unmatched_actual is None else paired_surplus + unmatched_actual
    )
    reconciliation = (
        None if mechanism_actual is None or tape.settlement_pnl is None
        else mechanism_actual - tape.settlement_pnl
    )
    if tape.status == "settled" and reconciliation is None:
        raise CompletionDataConflict(f"settled tape cannot reconcile: {tape.slug}")
    if reconciliation is not None and abs(reconciliation) > 1e-8:
        raise CompletionDataConflict(
            f"settlement mismatch for {tape.slug}: {reconciliation:+.12f}"
        )
    return ({
        "source": tape.source,
        "slug": tape.slug,
        "status": tape.status,
        "fills": len(tape.fills),
        "opened_shares": opened,
        "completed_shares": completed,
        "incomplete_shares": incomplete,
        "completion_rate": completed / opened if opened else None,
        "pair_surplus_usd": paired_surplus,
        "incomplete_cost_usd": incomplete_cost,
        "adverse_floor_usd": paired_surplus - incomplete_cost,
        "mechanism_actual_pnl_usd": mechanism_actual,
        "settlement_pnl_usd": tape.settlement_pnl,
        "reconciliation_delta_usd": reconciliation,
        "completion_delay_p50_s": weighted_quantile(delays, 0.5),
        "completion_delay_p90_s": weighted_quantile(delays, 0.9),
    }, delays)


def _number(row: dict[str, object], key: str) -> float:
    value = row[key]
    if not isinstance(value, (int, float)):
        raise TypeError(f"completion row {key} is not numeric")
    return float(value)


def analyze_databases(paths: Sequence[pathlib.Path], strategy: str) -> dict[str, object]:
    if not paths:
        raise ValueError("at least one paper database is required")
    tapes = [tape for path in paths for tape in _one_source(path, strategy)]
    selected, duplicates, unfinished_rows, unfinished_slugs = _select_tapes(tapes)
    windows: list[dict[str, object]] = []
    delays: list[tuple[float, float]] = []
    for tape in selected:
        row, row_delays = _window(tape)
        windows.append(row)
        delays.extend(row_delays)
    opened = sum(_number(row, "opened_shares") for row in windows)
    completed = sum(_number(row, "completed_shares") for row in windows)
    incomplete = sum(_number(row, "incomplete_shares") for row in windows)
    surplus = sum(_number(row, "pair_surplus_usd") for row in windows)
    incomplete_cost = sum(_number(row, "incomplete_cost_usd") for row in windows)
    surplus_per_completed = surplus / completed if completed else None
    cost_per_incomplete = incomplete_cost / incomplete if incomplete else None
    break_even = None
    if (surplus_per_completed is not None and surplus_per_completed > 0
            and cost_per_incomplete is not None and cost_per_incomplete >= 0):
        break_even = cost_per_incomplete / (
            cost_per_incomplete + surplus_per_completed
        )
    generator = pathlib.Path(__file__).resolve()
    return {
        "schema": "project-fail-pair-completion-v1",
        "generator": {
            "path": generator.relative_to(generator.parents[1]).as_posix(),
            "sha256": _sha256(generator),
        },
        "strategy": strategy,
        "sources": [
            {"path": str(path), "sha256": _sha256(path)} for path in paths
        ],
        "selection": {
            "finalized_windows": len(windows),
            "settled_windows": sum(row["status"] == "settled" for row in windows),
            "invalid_windows": sum(row["status"] == "invalid" for row in windows),
            "duplicate_finalized_rows_collapsed": duplicates,
            "unfinished_source_rows_excluded": unfinished_rows,
            "unfinished_slugs_without_finalization": unfinished_slugs,
        },
        "aggregate": {
            "opened_shares": opened,
            "completed_shares": completed,
            "incomplete_shares": incomplete,
            "completion_rate": completed / opened if opened else None,
            "completion_delay_p50_s": weighted_quantile(delays, 0.5),
            "completion_delay_p90_s": weighted_quantile(delays, 0.9),
            "pair_surplus_usd": surplus,
            "surplus_per_completed_share": surplus_per_completed,
            "incomplete_cost_usd": incomplete_cost,
            "cost_per_incomplete_share": cost_per_incomplete,
            "adverse_floor_usd": surplus - incomplete_cost,
            "zero_adverse_floor_completion_rate": break_even,
        },
        "windows": windows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("databases", nargs="+", type=pathlib.Path)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args(argv)
    result = analyze_databases(args.databases, args.strategy)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        if args.output.exists():
            raise FileExistsError(f"refusing to overwrite {args.output}")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
