"""Paper-trader A/B report: each strategy vs the recorder/sim baseline.

Run standalone:  python -m paper.report
Baseline (mm_sim v2 TWAP model, 116 recorded windows): win ~65%, ROC ~10%/window.
"""
from __future__ import annotations

import sqlite3

BASE_WIN = 0.65
BASE_ROC = 0.10
REDEMPTION_LOCK = 7200  # seconds: capital held to settlement stays locked ~2h until redemption


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
    # budget = peak SIMULTANEOUS capital-at-risk = bankroll you must hold in the account.
    # A buy deploys capital; a SELL recovers it immediately (round-trip); but capital left
    # in the HELD residual stays locked until redemption ~REDEMPTION_LOCK (~2h) after the
    # window settles. So round-trippers recover fast and barely lock anything, while pure
    # hold locks its whole stack for 2h -> needs far more. (For runs < 2h the hold-heavy
    # number is still climbing toward its steady-state peak, since nothing has redeemed yet.)
    fills = db.execute("SELECT ts, signed_cash FROM fills WHERE strategy=?", (strat,)).fetchall()
    if fills:
        ev = [(r[0], -r[1]) for r in fills]
        ev += [(r[0] + REDEMPTION_LOCK, r[1]) for r in db.execute(   # residual released 2h after settle
            "SELECT ts, cash FROM settlements WHERE strategy=? AND n_fills>0", (strat,))]
        ev.sort(key=lambda e: e[0])
        run = budget = 0.0
        for _, c in ev:
            run += c
            budget = max(budget, run)
    else:   # lock_arb: a complete set merges to $1 instantly, so capital recycles -> bankroll = peak single lock
        budget = db.execute("SELECT COALESCE(max(capital),0) FROM settlements WHERE strategy=?",
                            (strat,)).fetchone()[0]
    buys, sells = db.execute(
        "SELECT COALESCE(sum(buys),0), COALESCE(sum(sells),0) FROM settlements WHERE strategy=?",
        (strat,)).fetchone()
    volume = db.execute("SELECT COALESCE(sum(abs(signed_cash)),0) FROM fills WHERE strategy=?",
                        (strat,)).fetchone()[0]  # total $ traded (buys + sells notional)
    return {"strategy": strat, "settled": n, "win_rate": (wins / n if n else 0.0),
            "pnl": pnl, "roc_window": (pnl / cap_sum if cap_sum else 0.0),
            "budget": budget, "roc_budget": (pnl / budget if budget else 0.0),
            "buys": buys, "sells": sells, "volume": volume,
            "sell_buy": (sells / buys if buys else 0.0)}


def text(db_path: str = "paper/paper.db") -> str:
    db = sqlite3.connect(db_path)
    strats = _strategies(db)
    if not strats:
        warm = db.execute("SELECT count(*) FROM fills").fetchone()[0]
        return f"(warming up — no settled windows with fills yet; sim-fills so far: {warm})"
    snaps = sorted((snapshot_one(db, st) for st in strats), key=lambda s: s["pnl"], reverse=True)
    out = ["PAPER A/B — vs recorder baseline (win ~65%, ROC/win ~10%)",
           f"{'strategy':<13}{'windows':>8}{'fills':>7}{'vol$':>9}{'avg$':>7}{'win%':>6}{'pnl$':>9}{'budget$':>9}{'ROC/bud':>9}{'sell/buy':>9}"]
    for s in snaps:
        nf = s['buys'] + s['sells']
        out.append(f"{s['strategy']:<13}{s['settled']:>8}{nf:>7}{s['volume']:>9.0f}{(s['volume']/nf if nf else 0):>7.2f}"
                   f"{s['win_rate']*100:>5.0f}%{s['pnl']:>+9.1f}{s['budget']:>9.1f}"
                   f"{s['roc_budget']*100:>+8.0f}%{s['sell_buy']:>9.2f}")
    out.append("windows=settled; fills=buys+sells; budget$=peak capital-at-risk = bankroll needed")
    out.append("(sells recover instantly; HELD residual locked ~2h to redemption). sell/buy: 0=pure hold.")
    return "\n".join(out)


def tg_text(db_path: str = "paper/paper.db") -> str:
    """Phone-width (~36 char) monospace layout for Telegram. Full detail stays
    in the terminal report / logs."""
    import time as _t
    db = sqlite3.connect(db_path)
    strats = _strategies(db)
    if not strats:
        return "(warming up - no settled windows yet)"
    snaps = sorted((snapshot_one(db, st) for st in strats), key=lambda s: s["pnl"], reverse=True)
    hours = 0.0
    row = db.execute("SELECT min(ts), max(ts) FROM settlements WHERE n_fills>0").fetchone()
    if row and row[0]:
        hours = (row[1] - row[0]) / 3600
    out = [f"PAPER A/B v2.1 · {hours:.1f}h · {_t.strftime('%H:%M')}",
           f"{'strategy':<12}{'pnl$':>6}{'ROC%':>7}{'win':>5}{'bud$':>6}"]
    for s in snaps:
        roc = max(-9999, min(9999, s['roc_budget'] * 100))
        out.append(f"{s['strategy'][:12]:<12}{s['pnl']:>+6.0f}{roc:>+7.0f}"
                   f"{s['win_rate']*100:>4.0f}%{s['budget']:>6.0f}")
    out.append("ROC=pnl/bankroll needed. full cols in logs")
    return "\n".join(out)


if __name__ == "__main__":
    print(text())
