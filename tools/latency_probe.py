#!/usr/bin/env python3
"""Read-only Ireland-to-Polymarket latency probe; never submits an order."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import statistics
import sys
import time
import urllib.request
from collections import defaultdict

import websockets

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from live.feed_health import event_time_s

MKT_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def rest_rtts(samples: int = 15) -> list[float]:
    request = urllib.request.Request(
        "https://clob.polymarket.com/time",
        headers={"User-Agent": "project-fail-latency/2"},
    )
    values = []
    for _ in range(samples):
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
        values.append((time.perf_counter() - started) * 1000)
    return sorted(values)


async def ws_probe(
    token: str, seconds: float = 8.0,
) -> tuple[float, float | None, list[float], dict[str, list[float]]]:
    started = time.perf_counter()
    async with websockets.connect(MKT_WS, ping_interval=None, open_timeout=10) as ws:
        connected_ms = (time.perf_counter() - started) * 1000
        await ws.send(json.dumps({
            "assets_ids": [token], "type": "market", "custom_feature_enabled": True,
        }))
        first_ms = None
        last = None
        gaps: list[float] = []
        ages: dict[str, list[float]] = defaultdict(list)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=max(0.1, end - time.monotonic())
                )
            except asyncio.TimeoutError:
                break
            now = time.perf_counter()
            if first_ms is None:
                first_ms = (now - started) * 1000
            if last is not None:
                gaps.append((now - last) * 1000)
            last = now
            if raw == "PONG":
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            received_at = time.time()
            for event in payload if isinstance(payload, list) else [payload]:
                if not isinstance(event, dict):
                    continue
                event_at = event_time_s(event)
                if event_at is not None and -1 <= received_at - event_at <= 60:
                    ages[str(event.get("event_type") or "?")].append(
                        max(0.0, 1000 * (received_at - event_at))
                    )
    return connected_ms, first_ms, sorted(gaps), {
        kind: sorted(values) for kind, values in ages.items()
    }


def percentile(values: list[float], fraction: float) -> float:
    return values[min(len(values) - 1, int(len(values) * fraction))]


def active_btc_token() -> str | None:
    base = int(time.time() // 300) * 300
    slug = f"btc-updown-5m-{base}"
    request = urllib.request.Request(
        f"https://gamma-api.polymarket.com/events?slug={slug}",
        headers={"User-Agent": "project-fail-latency/2"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        return None
    markets = [market for event in payload if isinstance(event, dict)
               for market in (event.get("markets") or []) if isinstance(market, dict)]
    market = next((item for item in markets if item.get("slug") == slug), None)
    if market is None:
        return None
    tokens = market.get("clobTokenIds")
    tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
    return str(tokens[0]) if isinstance(tokens, list) and tokens else None


def main() -> None:
    rtts = rest_rtts()
    median = statistics.median(rtts)
    p90 = percentile(rtts, 0.9)
    print(f"REST GET RTT ms : min {rtts[0]:.0f} | median {median:.0f} | p90 {p90:.0f}")

    token = active_btc_token()
    if token is None:
        print("market WS       : active BTC token unavailable")
    else:
        seconds = float(os.environ.get("LATENCY_WS_SECONDS", "8"))
        connected, first, gaps, ages = asyncio.run(ws_probe(token, seconds))
        first_text = "none" if first is None else f"{first:.0f} ms"
        print(f"market WS       : connect {connected:.0f} ms | first frame {first_text}")
        if gaps:
            print(f"WS frame gaps   : median {statistics.median(gaps):.0f} | "
                  f"p90 {percentile(gaps, 0.9):.0f} ms | n={len(gaps)}")
        for kind, values in sorted(ages.items()):
            print(f"WS {kind:<16}: p50 {statistics.median(values):.0f} | "
                  f"p90 {percentile(values, 0.9):.0f} | max {values[-1]:.0f} ms | "
                  f"n={len(values)}")

    modeled = float(os.environ.get("PAPER_ACTION_LATENCY_MS", "65"))
    proxy = 5 * round(2 * p90 / 5)
    print(f"paper action ms : configured {modeled:.0f} | ~2x GET-p90 proxy {proxy}")
    print("Authenticated POST/cancel latency remains unmeasured; no order endpoint was called.")


if __name__ == "__main__":
    main()
