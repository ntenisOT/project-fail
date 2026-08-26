"""How early is a 5-minute window's outcome already decided by the TWAP?

These markets settle on a Chainlink 60-second TWAP: Up wins if the TWAP at
T+300 exceeds the TWAP at T+0 (paper/reference_report.py computes the same
signal at T+30). Because the settling number is a 60-second AVERAGE, a large
part of it is already observable before the window closes - at T+270, fully
half of the averaging period has happened.

That is a different claim from "Binance leads the TWAP", which we tested and
rejected (Binance right only 38-50% on disagreements). Leading requires
forecasting. Observability requires only arithmetic on a series we already
record at 1Hz, and it needs no hedge, no leverage and no exchange fees.

This measures, per checkpoint, how often the sign of the partial signal
matches the final outcome. If accuracy at T+270 is near 50%, the outcome is
genuinely undecided late and the idea is dead. If it is high, the next
question is whether Polymarket is already pricing it - which this does NOT
answer, and which decides whether any of it is tradeable.

Read-only.
"""
from __future__ import annotations

import argparse
import glob
import sqlite3
from collections import defaultdict

CHECKPOINTS = (120, 180, 240, 270, 285, 290, 295)


def load(path: str) -> dict[int, dict[int, float]]:
    """window_start -> {elapsed_second: twap_value}"""
    db = sqlite3.connect(path)
    try:
        rows = db.execute(
            "SELECT observed_at, value_e18 FROM reference_prices "
            "WHERE asset = 'btc' ORDER BY observed_at").fetchall()
    except sqlite3.Error:
        return {}
    per: dict[int, dict[int, float]] = defaultdict(dict)
    for observed_at, value in rows:
        ts = int(float(observed_at))
        start = ts // 300 * 300
        per[start][ts - start] = int(value) / 1e18
    return per


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbs", nargs="+", required=True,
                    help="paper db paths or globs (archives included)")
    args = ap.parse_args()

    windows: dict[int, dict[int, float]] = {}
    for pattern in args.dbs:
        for path in sorted(glob.glob(pattern)) or [pattern]:
            for start, series in load(path).items():
                windows.setdefault(start, {}).update(series)

    def nearest(series: dict[int, float], target: int, tol: int = 4) -> float | None:
        for delta in range(tol + 1):
            for elapsed in (target - delta, target + delta):
                if elapsed in series:
                    return series[elapsed]
        return None

    usable = 0
    hits: dict[int, list[bool]] = defaultdict(list)
    margins: dict[int, list[float]] = defaultdict(list)
    for start, series in sorted(windows.items()):
        opening = nearest(series, 0)
        final = nearest(series, 300)
        if opening is None or final is None or opening <= 0:
            continue
        outcome_up = final > opening
        if final == opening:
            continue
        usable += 1
        for cp in CHECKPOINTS:
            value = nearest(series, cp)
            if value is None:
                continue
            signal = value / opening - 1
            if signal == 0:
                continue
            hits[cp].append((signal > 0) == outcome_up)
            margins[cp].append(abs(signal) * 10_000)

    print(f"usable windows: {usable}\n")
    if not usable:
        print("no window has both an opening and a settling TWAP sample.")
        print("reference_prices needs to span a full window; run longer.")
        return

    print(f"{'checkpoint':<14}{'n':>6}{'agrees with outcome':>22}"
          f"{'median |signal|':>18}")
    for cp in CHECKPOINTS:
        flags = hits[cp]
        if not flags:
            continue
        acc = sum(flags) / len(flags)
        mg = sorted(margins[cp])
        med = mg[len(mg) // 2]
        left = 300 - cp
        print(f"T+{cp} ({left:>2}s left){len(flags):>6}{acc:>21.1%}"
              f"{med:>17.2f}bp")

    print("\n50% means the outcome is a coin flip at that moment and there is")
    print("nothing to observe. High accuracy means the settling number is")
    print("largely determined - but it is only an EDGE if Polymarket has not")
    print("already priced it, which this does not test.")


if __name__ == "__main__":
    main()
