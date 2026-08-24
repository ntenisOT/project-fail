"""Paper-trader A/B report: each strategy vs the recorder/sim baseline.

Run standalone:  python -m paper.report
Baseline (mm_sim v2 TWAP model, 116 recorded windows): win ~65%, ROC ~10%/window.
"""
from __future__ import annotations

import sqlite3

BASE_WIN = 0.65
BASE_ROC = 0.10
# xf twin races: original vs exit-first variant of the SAME signal (+ the two set-arbs)
PAIRS = [("roundtrip", "xf_roundtrip"), ("opp_size", "xf_opp"), ("neutral", "xf_neutral"),
         ("twap_confirm", "xf_twap_con"), ("twap_binance", "xf_twap_bin"),
         ("twap_deribit", "xf_twap_der"), ("binance_only", "xf_binance"),
         ("deribit_only", "xf_deribit"), ("lock_arb", "split_sell"),
         ("xf_twap_der", "ta_twap_der"), ("xf_neutral", "ta_neutral"), ("pair_mm", "ta_pair"),
         ("neutral", "lv_neutral"), ("pair_mm", "lv_pair"), ("ta_pair", "lv_ta_pair"),
         ("pair_mm", "hl_pair"), ("hl_pair", "lv_hl_pair"),
         ("hold", "lv_hold"), ("roundtrip", "lv_roundtrip"), ("rt_wide", "lv_rt_wide"),
         ("twap_confirm", "lv_twap_con"), ("twap_binance", "lv_twap_bin"), ("twap_deribit", "lv_twap_der"),
         ("binance_only", "lv_binance"), ("deribit_only", "lv_deribit"),
         ("xf_roundtrip", "lv_xf_rt"), ("xf_twap_der", "lv_xf_td"), ("xf_binance", "lv_xf_bin"),
         ("xf_deribit", "lv_xf_der"), ("xf_neutral", "lv_xf_neu"),
         ("ta_twap_der", "lv_ta_td"), ("ta_neutral", "lv_ta_neu"),
         ("lock_arb", "lock_fast"), ("split_sell", "split_fast"),
         ("sq_twap_con", "lv_twap_con"), ("sq_neutral", "lv_neutral"),
         ("sq_pair", "lv_pair"), ("sq_hl_pair", "lv_hl_pair")]


def _since(db, strat, t0):
    """windows/pnl/win%/sell-buy/resid for one strategy since t0 (same-period compare)."""
    r = db.execute("""SELECT count(*), COALESCE(sum(pnl),0),
                      COALESCE(sum(pnl > 0),0), COALESCE(sum(sells),0), COALESCE(sum(buys),0),
                      COALESCE(sum(resid_shares),0)
                      FROM settlements WHERE n_fills>0 AND strategy=? AND ts>=?""", (strat, t0)).fetchone()
    n, pnl, wins, sells, buys, resid = r
    return {"n": n, "pnl": pnl, "win": (wins / n if n else 0.0),
            "sb": (sells / buys if buys else 0.0), "resid": resid}


def pair_text(db) -> list[str]:
    t0 = db.execute("SELECT COALESCE(min(ts), 0) FROM settlements WHERE strategy LIKE 'xf_%' OR strategy='split_sell'").fetchone()[0]
    if not t0:
        return ["(xf twins warming up - no settled twin windows yet)"]
    import time as _t
    out = [f"XF TWIN RACES - both sides since {_t.strftime('%H:%M', _t.gmtime(t0))}Z (same tape, same signal; only inventory policy differs)",
           f"{'family':<14}{'orig pnl$':>10}{'xf pnl$':>9}{'edge$':>8}{'o.s/b':>7}{'xf.s/b':>7}{'o.resid':>9}{'xf.resid':>9}{'wins':>6}"]
    for orig, xf in PAIRS:
        o = _since(db, orig, t0)
        x = _since(db, xf, t0)
        if o["n"] == 0 and x["n"] == 0:
            continue
        out.append(f"{orig[:14]:<14}{o['pnl']:>+10.1f}{x['pnl']:>+9.1f}{x['pnl']-o['pnl']:>+8.1f}"
                   f"{o['sb']:>7.2f}{x['sb']:>7.2f}{o['resid']:>9.0f}{x['resid']:>9.0f}{min(o['n'],x['n']):>6}")
    out.append("edge$ = xf minus original | resid = shares carried into settlement (the carry)")
    return out
REDEMPTION_LOCK = 600   # seconds: MEASURED live 2026-08-24 - auto-redeem lands ~5-10 min after settle (old 2h assumption was wrong)


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
    import time as _t
    last = db.execute("SELECT max(ts) FROM settlements WHERE n_fills>0").fetchone()[0] or 0
    out = [f"PAPER A/B — vs recorder baseline (win ~65%, ROC/win ~10%) | last settle "
           f"{_t.strftime('%H:%M:%S', _t.gmtime(last))} UTC (5-min cycles)",
           f"{'strategy':<13}{'windows':>8}{'fills':>7}{'vol$':>9}{'avg$':>7}{'win%':>6}{'pnl$':>9}{'budget$':>9}{'ROC/bud':>9}{'sell/buy':>9}"]
    for s in snaps:
        nf = s['buys'] + s['sells']
        out.append(f"{s['strategy']:<13}{s['settled']:>8}{nf:>7}{s['volume']:>9.0f}{(s['volume']/nf if nf else 0):>7.2f}"
                   f"{s['win_rate']*100:>5.0f}%{s['pnl']:>+9.1f}{s['budget']:>9.1f}"
                   f"{s['roc_budget']*100:>+8.0f}%{s['sell_buy']:>9.2f}")
    out.append("windows=settled; fills=buys+sells; budget$=peak capital-at-risk = bankroll needed")
    out.append("(sells recover instantly; HELD residual locked ~10min to auto-redeem (measured)). sell/buy: 0=pure hold.")
    out.append("")
    out.extend(pair_text(db))
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
    last = row[1] or 0
    out = [f"PAPER A/B v2.1 · {hours:.1f}h · settled@{_t.strftime('%H:%M', _t.gmtime(last))}Z",
           f"{'strategy':<10}{'pnl$':>5}{'ROC%':>5}{'vol$':>6}{'avg$':>5}{'win%':>5}{'bud$':>5}"]
    for s in snaps:
        roc = max(-999, min(999, s['roc_budget'] * 100))
        nf = s['buys'] + s['sells']
        avg = s['volume'] / nf if nf else 0.0
        out.append(f"{s['strategy'][:10]:<10}{s['pnl']:>+5.0f}{roc:>+5.0f}{min(99999, s['volume']):>6.0f}"
                   f"{min(99.99, avg):>5.2f}{s['win_rate']*100:>4.0f}%{min(99999, s['budget']):>5.0f}")
    out.append("ROC=pnl/bankroll. full cols in logs")
    t0 = db.execute("SELECT COALESCE(min(ts),0) FROM settlements WHERE strategy LIKE 'xf_%' OR strategy='split_sell'").fetchone()[0]
    if t0:
        out.append("")
        out.append("XF RACES since launch (e=xf-orig)")
        for orig, xf in PAIRS:
            o = _since(db, orig, t0)
            x = _since(db, xf, t0)
            if x["n"] == 0 and o["n"] == 0:
                continue
            out.append(f"{orig[:11]:<11}{o['pnl']:>+6.0f}{x['pnl']:>+6.0f} e{x['pnl']-o['pnl']:>+5.0f}")
    return "\n".join(out)


if __name__ == "__main__":
    print(text())
