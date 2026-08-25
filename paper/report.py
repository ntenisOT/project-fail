"""Focused report for the queue-aware pair inventory experiment."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time


@dataclasses.dataclass(frozen=True)
class Snapshot:
    strategy: str
    windows: int
    trades: int
    volume: float
    pnl: float
    pair_edge: float
    unpaired_pnl: float
    win_rate: float
    bankroll: float
    roc: float
    buy_sum: float | None
    sell_sum: float | None
    unmatched: float
    posts_per_window: float
    rest_seconds: float
    queue_consumed: float
    action_ms: float
    post_only_rejects: int


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


def _bankroll(rows: list[tuple[float, str, float]]) -> float:
    """Conservative peak: each window's peak capital remains locked for 10m."""
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
        "SELECT ts,slug,pnl,capital,buys,sells FROM settlements WHERE strategy=?",
        (strategy,),
    ).fetchall()
    metrics = _metrics(db, strategy)
    windows = len(rows)
    pnl = sum(float(row[2]) for row in rows)
    trades = sum(int(row[4]) + int(row[5]) for row in rows)
    volume = float(db.execute(
        "SELECT COALESCE(sum(abs(signed_cash)),0) FROM fills WHERE strategy=?",
        (strategy,),
    ).fetchone()[0])
    bankroll = _bankroll([(float(row[0]), str(row[1]), float(row[3])) for row in rows])
    closed = metrics.get("closed_orders", 0.0)
    pair_edge = (metrics.get("buy_pair_shares", 0.0)
                 - metrics.get("buy_pair_cost", 0.0)
                 + metrics.get("sell_pair_proceeds", 0.0)
                 - metrics.get("sell_pair_shares", 0.0))
    return Snapshot(
        strategy=strategy, windows=windows, trades=trades, volume=volume, pnl=pnl,
        pair_edge=pair_edge, unpaired_pnl=pnl - pair_edge,
        win_rate=sum(float(row[2]) > 0 for row in rows) / windows if windows else 0.0,
        bankroll=bankroll, roc=pnl / bankroll if bankroll else 0.0,
        buy_sum=_sum_or_none(metrics.get("buy_pair_cost", 0.0),
                             metrics.get("buy_pair_shares", 0.0)),
        sell_sum=_sum_or_none(metrics.get("sell_pair_proceeds", 0.0),
                              metrics.get("sell_pair_shares", 0.0)),
        unmatched=metrics.get("unmatched_end", 0.0),
        posts_per_window=metrics.get("quote_posts", 0.0) / windows if windows else 0.0,
        rest_seconds=metrics.get("rest_seconds", 0.0) / closed if closed else 0.0,
        queue_consumed=metrics.get("queue_consumed", 0.0),
        action_ms=1000 * (_sum_or_none(metrics.get("action_seconds", 0.0),
                                      metrics.get("action_batches", 0.0)) or 0.0),
        post_only_rejects=int(metrics.get("post_only_rejects", 0.0)),
    )


def _format_sum(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def text(db_path: str = "paper/paper.db") -> str:
    db = sqlite3.connect(db_path)
    strategies = [row[0] for row in db.execute(
        "SELECT DISTINCT strategy FROM settlements ORDER BY strategy"
    )]
    if not strategies:
        fills = db.execute("SELECT count(*) FROM fills").fetchone()[0]
        db.close()
        return f"FOCUSED PAIR PAPER warming up | provisional fills={fills} | awaiting official outcomes"
    snapshots = sorted((snapshot_one(db, strategy) for strategy in strategies),
                       key=lambda row: row.pnl, reverse=True)
    last = float(db.execute("SELECT COALESCE(max(ts),0) FROM settlements").fetchone()[0])
    out = [
        f"FOCUSED PAIR PAPER | official outcomes | last settle {time.strftime('%H:%M:%S', time.gmtime(last))} UTC",
        f"{'strategy':<14}{'wnd':>5}{'trd':>6}{'vol$':>8}{'win%':>6}{'pnl$':>9}"
        f"{'edge$':>8}{'dir$':>8}{'bank$':>8}"
        f"{'ROC':>7}{'buySum':>8}{'sellSum':>9}{'unmat':>7}{'post/w':>8}{'rest':>7}"
        f"{'qAhead':>8}{'act':>8}{'reject':>8}",
    ]
    for row in snapshots:
        out.append(
            f"{row.strategy:<14}{row.windows:>5}{row.trades:>6}"
            f"{row.volume:>8.0f}{row.win_rate*100:>5.0f}%"
            f"{row.pnl:>+9.2f}{row.pair_edge:>+8.2f}{row.unpaired_pnl:>+8.2f}"
            f"{row.bankroll:>8.1f}"
            f"{row.roc*100:>+6.1f}%{_format_sum(row.buy_sum):>8}"
            f"{_format_sum(row.sell_sum):>9}{row.unmatched:>7.1f}"
            f"{row.posts_per_window:>8.1f}{row.rest_seconds:>6.1f}s"
            f"{row.queue_consumed:>8.0f}{row.action_ms:>6.0f}ms"
            f"{row.post_only_rejects:>8}"
        )
    out.extend((
        "buySum/sellSum are FIFO-matched opposite-token fills; unmat is end inventory.",
        "edge is hedged pair economics; dir is PnL left after edge (inventory/outcome).",
        "Queue-ahead depth is consumed before a maker fill; rebates are excluded.",
        "act is measured simulated action activation; reject is stale post-only prevention.",
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
                  key=lambda row: row.pnl, reverse=True)
    out = ["PAIR PAPER · queue-aware · no orders", f"{'strategy':<13}{'pnl':>7}{'ROC':>6}{'unm':>5}"]
    for row in rows:
        out.append(f"{row.strategy[:13]:<13}{row.pnl:>+7.1f}"
                   f"{row.roc*100:>+5.0f}%{row.unmatched:>5.0f}")
    result = "\n".join(out)
    db.close()
    return result


if __name__ == "__main__":
    print(text())
