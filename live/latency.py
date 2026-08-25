"""Measure read-only GET latency from this box to pipeline endpoints.

This does not measure order POST acknowledgement, executor polling, or a
cancel+replace cycle. Run: ``python -m live.latency``.
"""
from __future__ import annotations

import statistics
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
    for name, url in TARGETS.items():
        lat = probe(url)
        if not lat:
            print(f"{name:<26}   unreachable")
            continue
        p50 = statistics.median(lat)
        p95 = sorted(lat)[max(0, int(0.95 * len(lat)) - 1)]
        print(f"{name:<26}{p50:>8.0f}{p95:>8.0f}{len(lat):>4}")
    print("\nGET RTT is not end-to-end order latency; no PAPER_REQUOTE is inferred here.")


if __name__ == "__main__":
    main()
