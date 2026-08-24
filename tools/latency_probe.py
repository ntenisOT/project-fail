#!/usr/bin/env python3
"""Read-only latency probe (run on the deploy box). Measures:
  1) REST RTT to clob.polymarket.com  (the order-flight path)
  2) ws connect + book-event arrival cadence on one live token
  3) prints the end-to-end budget for today's pipeline vs an in-process one
No credentials, no orders, no state touched."""
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
    print()
    print("END-TO-END BUDGET (detect -> order at exchange), using median RTT:")
    print(f"  today's pipeline : book throttle 0-1000 (mean 500) + executor poll 0-1000 (mean 500)")
    print(f"                     + REST flight ~{med:.0f}  =>  mean ~{1000 + med:.0f} ms, worst ~{2000 + med:.0f} ms")
    print(f"  in-process ws    : event arrival ~30-80 + decide ~5 + REST flight ~{med:.0f}")
    print(f"                     =>  ~{65 + med:.0f}-{85 + med + 50:.0f} ms")


if __name__ == "__main__":
    main()
