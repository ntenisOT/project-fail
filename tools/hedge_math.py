"""Can a Binance/Deribit position hedge a Polymarket 5-minute binary?

The binary pays a FIXED +/-$0.50 on a $0.50 stake regardless of how far BTC
moves. A futures position pays PROPORTIONALLY to the move. You cannot flatten
a fixed payoff with a proportional one, so the question is only how badly the
mismatch bites, and what the hedge costs.

Uses the Chainlink 60s TWAP we already record - the exact series that settles
these markets, not a proxy.
"""
from __future__ import annotations

import sqlite3
import statistics
import sys

db = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "paper/paper.db")
rows = db.execute(
    "SELECT observed_at, value_e18 FROM reference_prices "
    "WHERE asset='btc' ORDER BY observed_at").fetchall()
series = [(float(t), int(v) / 1e18) for t, v in rows]
print(f"samples: {len(series)}   span: {(series[-1][0]-series[0][0])/60:.0f} min")
print(f"price: ${series[0][1]:,.0f} -> ${series[-1][1]:,.0f}")

by_ts = {int(t): p for t, p in series}
moves = []
for t, p in by_ts.items():
    q = by_ts.get(t + 300)
    if q:
        moves.append(abs(q - p) / p)
if not moves:
    raise SystemExit("not enough overlapping 5-minute pairs")

moves.sort()
mean_move = statistics.mean(moves)
med = statistics.median(moves)
p90 = moves[int(0.90 * len(moves))]
print(f"\n5-MINUTE |MOVE| on the settling TWAP  (n={len(moves)})")
print(f"  median {med*100:.4f}%   mean {mean_move*100:.4f}%   p90 {p90*100:.4f}%")

STAKE, PAYOFF = 0.50, 0.50
print(f"\nHEDGING ONE ${STAKE:.2f} BINARY (pays +/-${PAYOFF:.2f})")
for label, mv in (("median move", med), ("mean move", mean_move), ("p90 move", p90)):
    if mv <= 0:
        continue
    notional = PAYOFF / mv
    taker = notional * 0.0004      # Binance futures taker ~4bp
    maker = notional * 0.0002      # ~2bp
    print(f"  sized to the {label:<12} -> ${notional:,.0f} notional "
          f"({notional/STAKE:,.0f}x the stake)")
    print(f"     round-trip fee: ${2*taker:,.2f} taker / ${2*maker:,.2f} maker "
          f"vs ${PAYOFF:.2f} of binary upside")

notional = PAYOFF / mean_move
print(f"\nPAYOFF SHAPE at ${notional:,.0f} notional (sized to the mean move):")
for mult, name in ((0.25, "quiet"), (1.0, "typical"), (3.0, "violent")):
    r = mean_move * mult
    up = PAYOFF - notional * r
    down = -PAYOFF + notional * r
    print(f"  {name:<8} move {r*100:6.4f}%:  BTC up -> ${up:+.2f}   "
          f"BTC down -> ${down:+.2f}")
print("\nThe hedge is flat only at exactly the sized move. Below it you keep")
print("the binary swing unhedged; above it the futures leg overwhelms the")
print("capped binary. That is a straddle, not a hedge - and the fee is charged")
print("on the notional, not on the stake.")
