"""Causal T+30 signal audit for the official Chainlink 60-second TWAP."""

from __future__ import annotations

import dataclasses
import sqlite3
from decimal import Decimal


@dataclasses.dataclass(frozen=True)
class ReferenceSignal:
    asset: str
    start: int
    signal_bps: float
    source_age_ms: float
    outcome_up: bool
    first_mint_sold_up: bool | None

    @property
    def first_mint_toxic(self) -> bool | None:
        if self.first_mint_sold_up is None:
            return None
        return self.first_mint_sold_up == self.outcome_up


@dataclasses.dataclass(frozen=True)
class ReferenceMiss:
    asset: str
    start: int
    reason: str
    nearest_offset_ms: float | None


@dataclasses.dataclass(frozen=True)
class ReferenceAudit:
    total_windows: int
    signals: tuple[ReferenceSignal, ...]
    misses: tuple[ReferenceMiss, ...]


def audit(db: sqlite3.Connection) -> ReferenceAudit:
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reference_prices'"
    ).fetchone()
    if exists is None:
        return ReferenceAudit(0, (), ())
    result: list[ReferenceSignal] = []
    misses: list[ReferenceMiss] = []
    resolved_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resolved_windows'"
    ).fetchone()
    if resolved_exists is None:
        windows = db.execute(
            "SELECT DISTINCT asset,slug,outcome_up FROM settlements ORDER BY slug"
        ).fetchall()
    else:
        windows = db.execute(
            """SELECT asset,slug,outcome_up FROM resolved_windows
               UNION ALL
               SELECT DISTINCT s.asset,s.slug,s.outcome_up FROM settlements s
               WHERE NOT EXISTS (
                 SELECT 1 FROM resolved_windows r
                 WHERE r.asset=s.asset AND r.slug=s.slug)
               ORDER BY slug"""
        ).fetchall()
    for asset, slug, outcome_up in windows:
        try:
            start = int(str(slug).rsplit("-", 1)[1])
        except (IndexError, ValueError):
            continue
        decision_at = start + 30
        opening = db.execute(
            """SELECT value_e18 FROM reference_prices
               WHERE asset=? AND abs(observed_at-?)<0.001 AND received_at<=?
               ORDER BY received_at LIMIT 1""",
            (asset, start, decision_at),
        ).fetchone()
        if opening is None:
            exact_late = db.execute(
                """SELECT 1 FROM reference_prices
                   WHERE asset=? AND abs(observed_at-?)<0.001 LIMIT 1""",
                (asset, start),
            ).fetchone()
            nearest = db.execute(
                """SELECT observed_at FROM reference_prices
                   WHERE asset=? AND received_at<=?
                   ORDER BY abs(observed_at-?),received_at LIMIT 1""",
                (asset, decision_at, start),
            ).fetchone()
            misses.append(ReferenceMiss(
                str(asset), start,
                "opening_late" if exact_late is not None else "opening_missing",
                None if nearest is None else 1000 * (float(nearest[0]) - start),
            ))
            continue
        current = db.execute(
            """SELECT observed_at,value_e18 FROM reference_prices
               WHERE asset=? AND observed_at<=? AND received_at<=?
               ORDER BY observed_at DESC LIMIT 1""",
            (asset, decision_at, decision_at),
        ).fetchone()
        if current is None:
            misses.append(ReferenceMiss(str(asset), start, "decision_missing", None))
            continue
        opening_value = Decimal(str(opening[0]))
        current_value = Decimal(str(current[1]))
        if opening_value <= 0:
            continue
        first_fill = db.execute(
            """SELECT outcome_up FROM fills
               WHERE strategy='mintcycle5' AND slug=? ORDER BY ts LIMIT 1""",
            (slug,),
        ).fetchone()
        result.append(ReferenceSignal(
            str(asset), start,
            float((current_value / opening_value - 1) * Decimal(10_000)),
            max(0.0, 1000 * (decision_at - float(current[0]))),
            bool(outcome_up),
            None if first_fill is None else bool(first_fill[0]),
        ))
    return ReferenceAudit(len(windows), tuple(result), tuple(misses))


def snapshots(db: sqlite3.Connection) -> list[ReferenceSignal]:
    return list(audit(db).signals)
