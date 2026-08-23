"""Paper-trader A/B report: each strategy vs the recorder/sim baseline.

Run standalone:  python -m paper.report
Baseline (mm_sim v2 TWAP model, 116 recorded windows): win ~65%, ROC ~10%/window.
"""
from __future__ import annotations

import sqlite3

BASE_WIN = 0.65
BASE_ROC = 0.10


def _strategies(db):
    return [r[0] for r in db.execute(
        "SELECT DISTINCT strategy FROM settlements WHERE n_fills>0 ORDER BY strategy")]


def snapshot_one(db, strat: str) -> dict:
    settled = db.execute(
        "SELECT pnl, capital FROM settlements WHERE n_fills>0 AND strategy=?", (strat,)).fetchall()
    n = len(settled)
    wins = sum(1 for r in settled if r[0] > 0)
    pnl = sum(r[0] for r in settled)
    cap_sum = sum(r[1] for r in settled)
    # budget = peak concurrent capital deployed (signed-cash timeline for this strategy)
    ev = [(r[0], -r[1]) for r in db.execute(
        "SELECT ts, signed_cash FROM fills WHERE strategy=?", (strat,))]
    ev.sort()
    run = budget = 0.0
    for _, c in ev:
        run += c
        budget = max(budget, run)
    buys, sells = db.execute(
        "SELECT COALESCE(sum(buys),0), COALESCE(sum(sells),0) FROM settlements WHERE strategy=?",
        (strat,)).fetchone()
    return {"strategy": strat, "settled": n, "win_rate": (wins / n if n else 0.0),
            "pnl": pnl, "roc_window": (pnl / cap_sum if cap_sum else 0.0),
            "budget": budget, "roc_budget": (pnl / budget if budget else 0.0),
            "buys": buys, "sells": sells,
            "sell_buy": (sells / buys if buys else 0.0)}


def text(db_path: str = "paper/paper.db") -> str:
    db = sqlite3.connect(db_path)
    strats = _strategies(db)
    if not strats:
        warm = db.execute("SELECT count(*) FROM fills").fetchone()[0]
        return f"(warming up — no settled windows with fills yet; sim-fills so far: {warm})"
    out = ["PAPER A/B — strategies vs recorder baseline (win ~65%, ROC/window ~10%)",
           f"{'strategy':<12}{'wins':>7}{'win%':>7}{'pnl$':>9}{'ROC/win':>9}{'budget$':>9}{'ROC/bud':>9}{'sell/buy':>9}"]
    for st in strats:
        s = snapshot_one(db, st)
        out.append(f"{s['strategy']:<12}{s['settled']:>7}{s['win_rate']*100:>6.0f}%"
                   f"{s['pnl']:>+9.2f}{s['roc_window']*100:>+8.1f}%{s['budget']:>9.2f}"
                   f"{s['roc_budget']*100:>+8.1f}%{s['sell_buy']:>9.2f}")
    out.append("ROC/win = pnl/(sum per-window peak capital); ROC/bud = pnl/(peak concurrent capital).")
    out.append("sell/buy>0 = round-tripping (capital recycles, no 2h lock); 0 = pure hold.")
    return "\n".join(out)


if __name__ == "__main__":
    print(text())
