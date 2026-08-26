#!/usr/bin/env python3
"""Does Binance spot lead the Chainlink TWAP enough to beat the Polymarket price?

Context: tools/pair_cost_curve.py and a calibration test over ~1,955 BTC
windows showed the Polymarket price is essentially perfectly calibrated at
T+30/60/120/180 (z within +-0.5, bins 0-10% -> 2% and 90-100% -> 98%). So no
directional edge exists in the Polymarket price itself. Any edge must come
from information the market has not yet priced.

The market settles on the official Chainlink 60-second TWAP, so the only
credible external candidate is a venue that leads it. This reads the raw
cross-venue capture (RTDS TWAP60 + Binance bookTicker) and asks one question:

    at T+k inside a window, does the Binance move since the window opened
    predict the outcome BEYOND what the RTDS TWAP already shows?

If it does not, external-signal directional trading is dead for us and the
remaining options are execution-quality ones.

Run on the deploy box where the capture lives:
    ./.venv/bin/python -m tools.leadlag_probe --raw-dir out/<label>-raw
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import pathlib
import struct

MAGIC = b"PFRAWV2\n"
HEADER = struct.Struct("!QQI")


def iter_frames(path: pathlib.Path):
    """Yield (wall_ns, monotonic_ns, payload) from one raw frame chunk.

    The newest chunk is still being written by a live collector, so a
    truncated gzip tail is expected and is not an error.
    """
    try:
        with gzip.open(path, "rb") as handle:
            magic = handle.read(len(MAGIC))
            if magic != MAGIC:
                return
            while True:
                head = handle.read(HEADER.size)
                if len(head) < HEADER.size:
                    return
                wall_ns, mono_ns, length = HEADER.unpack(head)
                payload = handle.read(length)
                if len(payload) < length:
                    return
                yield wall_ns, mono_ns, payload
    except (EOFError, OSError, gzip.BadGzipFile):
        return


def collect(raw_dir: pathlib.Path, source: str):
    files = sorted(raw_dir.glob(f"*{source}-*.frames.gz"))
    for path in files:
        for wall_ns, _mono, payload in iter_frames(path):
            yield wall_ns, payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--offsets", default="30,60,120,180")
    args = ap.parse_args()
    raw = pathlib.Path(args.raw_dir)

    twap: list[tuple[float, float]] = []
    for wall_ns, payload in collect(raw, "polymarket_rtds"):
        try:
            msg = json.loads(payload)
            body = msg.get("payload") or {}
            if body.get("window_s") != 60:
                continue
            twap.append((wall_ns / 1e9, float(body["value"])))
        except (ValueError, KeyError, TypeError):
            continue
    spot: list[tuple[float, float]] = []
    for wall_ns, payload in collect(raw, "binance_spot"):
        try:
            data = json.loads(payload).get("data") or {}
            bid, ask = float(data["b"]), float(data["a"])
            spot.append((wall_ns / 1e9, (bid + ask) / 2))
        except (ValueError, KeyError, TypeError):
            continue
    twap.sort()
    spot.sort()
    print(f"twap60 samples={len(twap):,}  binance_spot samples={len(spot):,}")
    if len(twap) < 100 or len(spot) < 100:
        print("insufficient capture; let the collector run longer")
        return 0
    print(f"span: {twap[0][0]:.0f}..{twap[-1][0]:.0f} "
          f"({(twap[-1][0]-twap[0][0])/3600:.1f}h)")

    def at(series, t):
        lo, hi = 0, len(series) - 1
        if t < series[0][0] or t > series[-1][0]:
            return None
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if series[mid][0] <= t:
                lo = mid
            else:
                hi = mid - 1
        return series[lo][1]

    base0 = int(twap[0][0] // 300) * 300 + 300
    base1 = int(twap[-1][0] // 300) * 300 - 300
    offsets = [int(x) for x in args.offsets.split(",")]
    print(f"\n{'T+k':>5} {'n':>5} {'twap-only':>11} {'binance adds':>13} {'z(extra)':>9}")
    for k in offsets:
        rows = []
        for start in range(base0, base1 + 1, 300):
            t0 = at(twap, start)
            tk = at(twap, start + k)
            te = at(twap, start + 300)
            s0 = at(spot, start)
            sk = at(spot, start + k)
            if None in (t0, tk, te, s0, sk) or t0 <= 0 or s0 <= 0:
                continue
            outcome = 1.0 if te >= t0 else 0.0
            rows.append(((tk - t0) / t0, (sk - s0) / s0, outcome))
        if len(rows) < 20:
            print(f"{k:>5} {len(rows):>5}   (too few resolved windows)")
            continue
        n = len(rows)
        twap_hit = sum(1 for a, _b, o in rows if (a >= 0) == (o == 1.0)) / n
        # windows where the two signals DISAGREE: does binance win those?
        dis = [(a, b, o) for a, b, o in rows if (a >= 0) != (b >= 0)]
        if dis:
            binance_wins = sum(1 for _a, b, o in dis if (b >= 0) == (o == 1.0))
            p = binance_wins / len(dis)
            z = (p - 0.5) / (0.5 / math.sqrt(len(dis)))
            extra = f"{binance_wins}/{len(dis)} = {100*p:.0f}%"
        else:
            extra, z = "no disagreement", 0.0
        print(f"{k:>5} {n:>5} {100*twap_hit:>10.1f}% {extra:>13} {z:>+9.2f}")
    print("\nz>1.96 on the disagreement set would mean Binance carries information")
    print("the official TWAP has not yet reflected - the only remaining source of")
    print("a directional edge, since the Polymarket price itself is calibrated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
