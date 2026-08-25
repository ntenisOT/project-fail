#!/usr/bin/env python3
"""Read-only latency probe for the deployed quote pipeline.

Measures CLOB GET RTT, market-feed cadence, and the actual age of new quote
intents when observed by a one-second executor-style poll. It deliberately does
not call order endpoints, so POST acknowledgement and cancel/replace time remain
unknown rather than being invented from GET latency.
"""
import asyncio
import json
import re
import statistics
import time
import urllib.request

import websockets


def rest_rtt(n=15):
    rtts = []
    req = urllib.request.Request("https://clob.polymarket.com/time",
                                 headers={"User-Agent": "Mozilla/5.0"})
    for _ in range(n):
        t0 = time.time()
        urllib.request.urlopen(req, timeout=5).read()
        rtts.append((time.time() - t0) * 1000)
    rtts.sort()
    return rtts


def live_token():
    lines = open("paper/intents.jsonl", encoding="utf-8").readlines()[-800:]
    for line in reversed(lines):
        try:
            it = json.loads(line)
        except ValueError:
            continue
        if it.get("type") == "book" and it.get("token"):
            return it["token"]
    return None


def intent_poll_age(seconds=20.0, poll_s=1.0):
    """Observe new quote intents using the executor's current polling cadence."""
    ages = []
    with open("paper/intents.jsonl", encoding="utf-8") as source:
        source.seek(0, 2)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(min(poll_s, max(0.0, end - time.monotonic())))
            observed_at = time.time()
            for raw in source.readlines():
                try:
                    item = json.loads(raw)
                    if item.get("strategy") and item.get("ts"):
                        ages.append(max(0.0, observed_at - float(item["ts"])) * 1000)
                except (TypeError, ValueError):
                    continue
    return sorted(ages)


async def ws_probe(token, seconds=8.0):
    src = open("paper/run.py", encoding="utf-8").read()
    url = re.search(r'MKT_WS\s*=\s*"([^"]+)"', src).group(1)
    t0 = time.time()
    async with websockets.connect(url, ping_interval=None, open_timeout=10) as ws:
        t_conn = (time.time() - t0) * 1000
        await ws.send(json.dumps({"assets_ids": [token], "type": "market"}))
        first = None
        gaps = []
        last = None
        end = time.time() + seconds
        while time.time() < end:
            try:
                await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
            except asyncio.TimeoutError:
                break
            now = time.time()
            if first is None:
                first = (now - t0) * 1000
            if last is not None:
                gaps.append((now - last) * 1000)
            last = now
    return t_conn, first, gaps


def main():
    r = rest_rtt()
    med = r[len(r) // 2]
    print(f"REST RTT ms      : min {r[0]:.0f} | median {med:.0f} | p90 {r[int(len(r)*0.9)]:.0f}   (n={len(r)})")
    tok = live_token()
    if tok:
        t_conn, first, gaps = asyncio.run(ws_probe(tok))
        if gaps:
            gaps.sort()
            print(f"ws connect ms    : {t_conn:.0f} | first event after connect: {first:.0f}")
            print(f"ws event gaps ms : median {gaps[len(gaps)//2]:.0f} | p90 {gaps[int(len(gaps)*0.9)]:.0f} | n={len(gaps)} in 8s")
        else:
            print(f"ws connect ms    : {t_conn:.0f} | no events in window (quiet token)")
    else:
        print("no live token found in intents.jsonl - ws probe skipped")
    ages = intent_poll_age()
    if ages:
        p50 = statistics.median(ages)
        p90 = ages[min(len(ages) - 1, int(len(ages) * 0.9))]
        print(f"intent -> 1s poll : median {p50:.0f} | p90 {p90:.0f} | n={len(ages)}")
        print(f"new-order lower bound (poll + GET RTT proxy): median {p50 + med:.0f} ms")
    else:
        print("intent -> 1s poll : no new strategy intents observed")
    print("POST acknowledgement and cancel+replace latency are unmeasured; GET RTT is only a lower-bound proxy.")


if __name__ == "__main__":
    main()
