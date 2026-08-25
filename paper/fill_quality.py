"""Aggregate maker fill-age and adverse-selection evidence from paper ledgers."""

from __future__ import annotations

import dataclasses
import json
import sqlite3

from paper.pair_lots import weighted_quantile


@dataclasses.dataclass(frozen=True)
class FillQualitySnapshot:
    strategy: str
    shares: float
    age_p50_s: float
    age_p90_s: float
    markout_1_cents: float
    coverage_1: float
    markout_5_cents: float
    coverage_5: float
    markout_15_cents: float
    coverage_15: float


def _weighted_mean(samples: list[tuple[float, float]]) -> float:
    weight = sum(shares for _, shares in samples)
    return sum(value * shares for value, shares in samples) / weight if weight else 0.0


def snapshots(db: sqlite3.Connection) -> list[FillQualitySnapshot]:
    ages: dict[str, list[tuple[float, float]]] = {}
    marks: dict[str, dict[str, list[tuple[float, float]]]] = {}
    for strategy, raw in db.execute("SELECT strategy,data FROM window_metrics"):
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(values, dict):
            continue
        age_rows = values.get("maker_fill_ages")
        if isinstance(age_rows, list):
            for row in age_rows:
                if isinstance(row, (list, tuple)) and len(row) == 2:
                    try:
                        age, shares = float(row[0]), float(row[1])
                    except (TypeError, ValueError):
                        continue
                    if age >= 0 and shares > 0:
                        ages.setdefault(str(strategy), []).append((age, shares))
        markouts = values.get("maker_markouts")
        if not isinstance(markouts, dict):
            continue
        for horizon in ("1", "5", "15"):
            rows = markouts.get(horizon)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, (list, tuple)) and len(row) == 3:
                    try:
                        edge, shares = float(row[0]), float(row[1])
                    except (TypeError, ValueError):
                        continue
                    if shares > 0:
                        marks.setdefault(str(strategy), {}).setdefault(
                            horizon, [],
                        ).append((edge, shares))
    result: list[FillQualitySnapshot] = []
    for strategy, age_samples in sorted(ages.items()):
        shares = sum(weight for _, weight in age_samples)
        by_horizon = marks.get(strategy, {})
        fields: list[float] = []
        for horizon in ("1", "5", "15"):
            samples = by_horizon.get(horizon, [])
            covered = sum(weight for _, weight in samples)
            fields.extend((100 * _weighted_mean(samples), covered / shares if shares else 0.0))
        result.append(FillQualitySnapshot(
            strategy, shares,
            weighted_quantile(age_samples, 0.5),
            weighted_quantile(age_samples, 0.9),
            *fields,
        ))
    return result
