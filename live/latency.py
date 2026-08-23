"""Measure real round-trip latency from THIS box to every endpoint the pipeline
touches, and suggest the PAPER_REQUOTE the paper model should use so sim fills
match this deployment's reality. Read-only. Run: python -m live.latency"""
from __future__ import annotations

import statistics
import sys
import time
import urllib.request

TARGETS = {
    "clob REST (order path)": "https://clob.polymarket.com/time",
    "gamma (discovery)": "https://gamma-api.polymarket.com/events?slug=x",
    "binance REST (signal)": "https://api.binance.com/api/v3/time",
    "deribit REST (signal)": "https://www.deribit.com/api/v2/public/get_time",
}
N = 12


def probe(url):
    lat = []
    for i in range(N):
        t0 = time.perf_counter()
        try:
            urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10).read()
        except Exception:
            continue
        lat.append((time.perf_counter() - t0) * 1000)
    return lat[1:] if len(lat) > 1 else lat      # drop connection-setup outlier


def main():
    print(f"{'endpoint':<26}{'p50 ms':>8}{'p95 ms':>8}{'n':>4}")
    clob_p50 = None
    for name, url in TARGETS.items():
        lat = probe(url)
        if not lat:
            print(f"{name:<26}   unreachable")
            continue
        p50 = statistics.median(lat)
        p95 = sorted(lat)[max(0, int(0.95 * len(lat)) - 1)]
        if "clob" in name:
            clob_p50 = p50
        print(f"{name:<26}{p50:>8.0f}{p95:>8.0f}{len(lat):>4}")
    if clob_p50 is not None:
        # realistic requote = see the trigger + decide + cancel + place = ~2 CLOB round trips + slack
        req = max(0.05, round((2 * clob_p50 / 1000) + 0.05, 2))
        print(f"\nsuggested PAPER_REQUOTE for this box: {req:.2f}  (2x CLOB RTT + 50ms loop slack)")
        print("set it in .env, restart paper runner -> sim fills assume THIS box's speed.")


if __name__ == "__main__":
    main()
