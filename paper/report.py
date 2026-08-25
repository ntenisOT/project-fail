"""Focused report for the queue-aware pair inventory experiment."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time

from paper.feed_quality import snapshots as feed_quality_snapshots
from paper.pair_lots import weighted_quantile


@dataclasses.dataclass(frozen=True)
class Snapshot:
    strategy: str
    windows: int
    trades: int
    volume: float
    pnl: float
    pair_edge: float
    neutral_pnl: float
    outcome_pnl: float
    worst_pnl: float
    win_rate: float
    bankroll: float
    roc: float
    buy_sum: float | None
    sell_sum: float | None
    taker_fees: float
    maker_rebates: float
    unmatched: float
    posts_per_window: float
    rest_seconds: float
    queue_consumed: float
    action_ms: float
    post_only_rejects: int
    pre_activation_trades: int
    pair_delay_p50_s: float
    pair_delay_p90_s: float


@dataclasses.dataclass(frozen=True)
class AssetSnapshot:
    strategy: str
    asset: str
    windows: int
    pnl: float
    neutral_pnl: float
    worst_pnl: float
    maker_rebates: float
    unmatched: float


def _metrics(db: sqlite3.Connection, strategy: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    rows = db.execute("SELECT data FROM window_metrics WHERE strategy=?", (strategy,))
    for (raw,) in rows:
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        for key, value in values.items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0.0) + float(value)
    return totals


def _pair_delays(db: sqlite3.Connection, strategy: str) -> tuple[float, float]:
    samples: list[tuple[float, float]] = []
    rows = db.execute("SELECT data FROM window_metrics WHERE strategy=?", (strategy,))
    for (raw,) in rows:
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(values, dict):
            continue
        for key in ("buy_pair_delays", "sell_pair_delays"):
            delays = values.get(key)
            if not isinstance(delays, list):
                continue
            for row in delays:
                if not isinstance(row, (list, tuple)) or len(row) != 2:
                    continue
                try:
                    delay, shares = float(row[0]), float(row[1])
                except (TypeError, ValueError):
                    continue
                if delay >= 0 and shares > 0:
                    samples.append((delay, shares))
    return weighted_quantile(samples, 0.5), weighted_quantile(samples, 0.9)


def _bankroll(rows: list[tuple[float, str, float]]) -> float:
    """Conservative peak: each window's peak capital remains locked for 15m."""
    events: list[tuple[float, float]] = []
    for settled_at, slug, capital in rows:
        try:
            start = float(slug.rsplit("-", 1)[1])
        except (IndexError, ValueError):
            start = settled_at - 300
        events.extend(((start, capital), (start + 900, -capital)))
    running = peak = 0.0
    for _, delta in sorted(events):
        running += delta
        peak = max(peak, running)
    return peak


def _sum_or_none(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def snapshot_one(db: sqlite3.Connection, strategy: str) -> Snapshot:
    rows = db.execute(
        """SELECT ts,slug,pnl,capital,buys,sells,cash,residual,resid_shares
           FROM settlements WHERE strategy=?""",
        (strategy,),
    ).fetchall()
    metrics = _metrics(db, strategy)
    pair_delay_p50_s, pair_delay_p90_s = _pair_delays(db, strategy)
    windows = len(rows)
    if any(float(row[7]) < -1e-8 or float(row[8]) - float(row[7]) < -1e-8
           for row in rows):
        raise RuntimeError(f"negative paper inventory for {strategy}; generation is invalid")
    pnl = sum(float(row[2]) for row in rows)
    neutral_pnl = sum(float(row[6]) + float(row[8]) / 2 for row in rows)
    worst_pnl = sum(float(row[6]) + min(float(row[7]), float(row[8]) - float(row[7]))
                    for row in rows)
    trades = sum(int(row[4]) + int(row[5]) for row in rows)
    volume = float(db.execute(
        """SELECT COALESCE(sum(abs(signed_cash)),0) FROM fills
           WHERE strategy=? AND slug IN (
             SELECT slug FROM settlements WHERE strategy=?
           )""",
        (strategy, strategy),
    ).fetchone()[0])
    bankroll = _bankroll([(float(row[0]), str(row[1]), float(row[3])) for row in rows])
    closed = metrics.get("closed_orders", 0.0)
    pair_edge = (metrics.get("buy_pair_shares", 0.0)
                 - metrics.get("buy_pair_cost", 0.0)
                 + metrics.get("sell_pair_proceeds", 0.0)
                 - metrics.get("sell_pair_shares", 0.0))
    return Snapshot(
        strategy=strategy, windows=windows, trades=trades, volume=volume, pnl=pnl,
        pair_edge=pair_edge, neutral_pnl=neutral_pnl,
        outcome_pnl=pnl - neutral_pnl, worst_pnl=worst_pnl,
        win_rate=sum(float(row[2]) > 0 for row in rows) / windows if windows else 0.0,
        bankroll=bankroll, roc=pnl / bankroll if bankroll else 0.0,
        buy_sum=_sum_or_none(metrics.get("buy_pair_cost", 0.0),
                             metrics.get("buy_pair_shares", 0.0)),
        sell_sum=_sum_or_none(metrics.get("sell_pair_proceeds", 0.0),
                              metrics.get("sell_pair_shares", 0.0)),
        taker_fees=metrics.get("taker_fees", 0.0),
        maker_rebates=metrics.get("maker_rebates", 0.0),
        unmatched=metrics.get("unmatched_end", 0.0),
        posts_per_window=metrics.get("quote_posts", 0.0) / windows if windows else 0.0,
        rest_seconds=metrics.get("rest_seconds", 0.0) / closed if closed else 0.0,
        queue_consumed=metrics.get("queue_consumed", 0.0),
        action_ms=1000 * (_sum_or_none(metrics.get("action_seconds", 0.0),
                                      metrics.get("action_batches", 0.0)) or 0.0),
        post_only_rejects=int(metrics.get("post_only_rejects", 0.0)),
        pre_activation_trades=int(metrics.get("pre_activation_trades", 0.0)),
        pair_delay_p50_s=pair_delay_p50_s,
        pair_delay_p90_s=pair_delay_p90_s,
    )


def asset_snapshots(db: sqlite3.Connection) -> list[AssetSnapshot]:
    rebates: dict[tuple[str, str], float] = {}
    for strategy, asset, raw in db.execute(
        "SELECT strategy,asset,data FROM window_metrics"
    ):
        try:
            value = float(json.loads(raw).get("maker_rebates", 0.0))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            continue
        key = (str(strategy), str(asset))
        rebates[key] = rebates.get(key, 0.0) + value
    rows = db.execute(
        """SELECT strategy,asset,count(*),sum(pnl),
                  sum(cash + resid_shares/2),
                  sum(cash + min(residual,resid_shares-residual)),
                  sum(abs(2*residual-resid_shares))
           FROM settlements GROUP BY strategy,asset ORDER BY strategy,asset"""
    )
    return [AssetSnapshot(str(strategy), str(asset), int(windows), float(pnl),
                          float(neutral), float(worst),
                          rebates.get((str(strategy), str(asset)), 0.0), float(unmatched))
            for strategy, asset, windows, pnl, neutral, worst, unmatched in rows]


def _format_sum(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _integrity_lines(db: sqlite3.Connection) -> list[str]:
    scored = int(db.execute("SELECT count(*) FROM settlements").fetchone()[0])
    has_invalid_audit = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='invalid_windows'"
    ).fetchone()
    if has_invalid_audit is None:
        return (["cohort integrity | invalid-window audit unavailable for legacy DB"]
                if scored else [])
    invalid, invalid_fills, max_capital, max_lag_ms = db.execute(
        """SELECT count(*),COALESCE(sum(n_fills),0),COALESCE(max(capital),0),
                  COALESCE(max(event_lag_ms),0)
           FROM invalid_windows"""
    ).fetchone()
    invalid = int(invalid)
    if not scored and not invalid:
        return []
    total = scored + invalid
    reasons = ", ".join(
        f"{reason}={count}" for reason, count in db.execute(
            "SELECT reason,count(*) FROM invalid_windows GROUP BY reason ORDER BY reason"
        )
    ) or "none"
    return [
        f"cohort integrity | scored strategy-windows={scored} | invalid={invalid} | "
        f"validity={100 * scored / total:.1f}% | invalid fills={int(invalid_fills)} | "
        f"max invalid committed=${float(max_capital):.2f} | "
        f"max invalid event lag={float(max_lag_ms):.0f}ms",
        f"invalid reasons | {reasons}",
    ]


def text(db_path: str = "paper/paper.db") -> str:
    db = sqlite3.connect(db_path)
    strategies = [row[0] for row in db.execute(
        "SELECT DISTINCT strategy FROM settlements ORDER BY strategy"
    )]
    if not strategies:
        fills = db.execute("SELECT count(*) FROM fills").fetchone()[0]
        integrity = _integrity_lines(db)
        db.close()
        return "\n".join([
            f"FOCUSED PAIR PAPER warming up | provisional fills={fills} | awaiting official outcomes",
            *integrity,
        ])
    snapshots = sorted((snapshot_one(db, strategy) for strategy in strategies),
                       key=lambda row: row.neutral_pnl, reverse=True)
    last = float(db.execute("SELECT COALESCE(max(ts),0) FROM settlements").fetchone()[0])
    out = [
        f"FOCUSED PAIR PAPER | official outcomes | last settle {time.strftime('%H:%M:%S', time.gmtime(last))} UTC",
        f"{'strategy':<14}{'wnd':>5}{'trd':>6}{'vol$':>8}{'win%':>6}{'pnl$':>9}"
        f"{'edge$':>8}{'neutral$':>9}{'outcome$':>9}{'worst$':>8}{'bank$':>8}"
        f"{'ROC':>7}{'buySum':>8}{'sellSum':>9}{'fee$':>7}{'rebate$':>9}"
        f"{'unmat':>7}{'post/w':>8}{'rest':>7}"
        f"{'qAhead':>8}{'act':>8}{'reject':>8}{'preAct':>8}"
        f"{'d50':>8}{'d90':>8}",
    ]
    for row in snapshots:
        out.append(
            f"{row.strategy:<14}{row.windows:>5}{row.trades:>6}"
            f"{row.volume:>8.0f}{row.win_rate*100:>5.0f}%"
            f"{row.pnl:>+9.2f}{row.pair_edge:>+8.2f}{row.neutral_pnl:>+9.2f}"
            f"{row.outcome_pnl:>+9.2f}"
            f"{row.worst_pnl:>+8.2f}{row.bankroll:>8.1f}"
            f"{row.roc*100:>+6.1f}%{_format_sum(row.buy_sum):>8}"
            f"{_format_sum(row.sell_sum):>9}{row.taker_fees:>7.2f}"
            f"{row.maker_rebates:>9.2f}{row.unmatched:>7.1f}"
            f"{row.posts_per_window:>8.1f}{row.rest_seconds:>6.1f}s"
            f"{row.queue_consumed:>8.0f}{row.action_ms:>6.0f}ms"
            f"{row.post_only_rejects:>8}{row.pre_activation_trades:>8}"
            f"{row.pair_delay_p50_s:>7.1f}s{row.pair_delay_p90_s:>7.1f}s"
        )
    out.extend(_integrity_lines(db))
    out.extend((
        "feed-quality breakdown; lagged windows retain measured feed-tail exposure",
        f"{'strategy':<14}{'quality':<14}{'wnd':>5}{'pnl$':>10}"
        f"{'neutral$':>10}{'worst$':>10}{'unmat':>8}{'maxLag':>9}",
    ))
    for quality_row in feed_quality_snapshots(db):
        out.append(
            f"{quality_row.strategy:<14}{quality_row.quality:<14}{quality_row.windows:>5}"
            f"{quality_row.pnl:>+10.2f}{quality_row.neutral_pnl:>+10.2f}"
            f"{quality_row.worst_pnl:>+10.2f}{quality_row.unmatched:>8.1f}"
            f"{quality_row.max_lag_ms:>8.0f}ms"
        )
    out.extend((
        "asset breakdown; pnl/neutral/worst keep directional luck and concentration visible",
        f"{'strategy':<14}{'asset':<6}{'wnd':>5}{'pnl$':>10}"
        f"{'neutral$':>10}{'worst$':>10}{'rebate$':>10}{'unmat':>8}",
    ))
    for asset_row in asset_snapshots(db):
        out.append(f"{asset_row.strategy:<14}{asset_row.asset:<6}{asset_row.windows:>5}"
                   f"{asset_row.pnl:>+10.2f}{asset_row.neutral_pnl:>+10.2f}"
                   f"{asset_row.worst_pnl:>+10.2f}{asset_row.maker_rebates:>10.2f}"
                   f"{asset_row.unmatched:>8.1f}")
    out.extend((
        "buySum/sellSum are FIFO-matched opposite-token fills; unmat is end inventory.",
        "pair completion d50/d90 are share-weighted FIFO delays between opposite fills.",
        "edge is FIFO-paired economics; neutral marks every end token at 50 cents.",
        "outcome is realized PnL minus neutral, isolating settlement-direction luck.",
        "fee$ is taker fee; rebate$ is the documented 20% maker baseline, not payout truth.",
        "worst is settlement PnL under the adverse outcome for every asset-window.",
        "Queue-ahead depth is consumed before a maker fill; rebates are excluded.",
        "act is measured simulated action activation; reject is stale post-only prevention.",
        "preAct counts delayed trade events rejected because they predate order activation.",
    ))
    result = "\n".join(out)
    db.close()
    return result


def tg_text(db_path: str = "paper/paper.db") -> str:
    db = sqlite3.connect(db_path)
    strategies = [row[0] for row in db.execute(
        "SELECT DISTINCT strategy FROM settlements ORDER BY strategy"
    )]
    if not strategies:
        db.close()
        return "PAIR PAPER warming up - official outcomes pending"
    rows = sorted((snapshot_one(db, strategy) for strategy in strategies),
                  key=lambda row: row.neutral_pnl, reverse=True)
    out = ["PAIR PAPER · queue-aware · no orders",
           f"{'strategy':<13}{'pnl':>7}{'neutral':>8}{'out':>7}{'unm':>5}"]
    for row in rows:
        out.append(f"{row.strategy[:13]:<13}{row.pnl:>+7.1f}"
                   f"{row.neutral_pnl:>+8.1f}{row.outcome_pnl:>+7.1f}"
                   f"{row.unmatched:>5.0f}")
    result = "\n".join(out)
    db.close()
    return result


if __name__ == "__main__":
    print(text())
