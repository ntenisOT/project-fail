#!/usr/bin/env python3
"""
Recorder v2 — captures the two feeds needed to replicate the winners' strategy:

  1. Chainlink reference prices (topic crypto_prices_chainlink) + Binance spot
     (crypto_prices) over wss://ws-live-data.polymarket.com  -> out/refprices-*.jsonl
     (compute the 60s TWAP fair value offline from these ticks)
  2. FULL-DEPTH order book (all levels) for each current 5-min window's Up & Down
     tokens, polled from the CLOB /book REST endpoint  -> out/depth-*.jsonl

Read-only. No orders, no wallet. Ctrl-C to stop. Runs forever, reconnecting.
"""
import asyncio, json, os, sys, time, urllib.request
from datetime import datetime, timezone
import websockets

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ASSETS = {
    "btc": {"prefix": "btc-updown-5m", "cl": "btc/usd"},
    "eth": {"prefix": "eth-updown-5m", "cl": "eth/usd"},
    "sol": {"prefix": "sol-updown-5m", "cl": "sol/usd"},
    "xrp": {"prefix": "xrp-updown-5m", "cl": "xrp/usd"},
}
WS_LIVE = "wss://ws-live-data.polymarket.com"
CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
OUTDIR = "out"
LEVELS = 20

os.makedirs(OUTDIR, exist_ok=True)
_counts = {"ref": 0, "depth": 0}


def append(kind, obj):
    fn = f"{kind}-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    with open(os.path.join(OUTDIR, fn), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def http_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def current_window(prefix, now):
    base = int(now - (now % 300))
    try:
        ev = http_json(f"{GAMMA}/events?slug={prefix}-{base}")
    except Exception:
        return None
    if not ev:
        return None
    m = (ev[0].get("markets") or [{}])[0]
    ids = m.get("clobTokenIds")
    if isinstance(ids, str):
        ids = json.loads(ids)
    if not ids or len(ids) < 2:
        return None
    return {"slug": f"{prefix}-{base}", "start": base, "end": base + 300,
            "up_id": ids[0], "down_id": ids[1]}


def fetch_book(token_id):
    try:
        b = http_json(f"{CLOB}/book?token_id={token_id}")
        bids = [[float(x["price"]), float(x["size"])] for x in (b.get("bids") or [])]
        asks = [[float(x["price"]), float(x["size"])] for x in (b.get("asks") or [])]
        bids.sort(key=lambda z: -z[0]); asks.sort(key=lambda z: z[0])
        return {"bids": bids[:LEVELS], "asks": asks[:LEVELS]}
    except Exception:
        return None


async def task_chainlink():
    while True:
        try:
            async with websockets.connect(WS_LIVE, ping_interval=None, open_timeout=12) as ws:
                for topic in ["crypto_prices_chainlink", "crypto_prices"]:
                    await ws.send(json.dumps({"action": "subscribe",
                        "subscriptions": [{"topic": topic, "type": "update"}]}))
                last_ping = 0.0
                while True:
                    if time.time() - last_ping > 5:
                        await ws.send("PING"); last_ping = time.time()
                    msg = await asyncio.wait_for(ws.recv(), timeout=15)
                    try:
                        d = json.loads(msg)
                    except Exception:
                        continue
                    topic = d.get("topic"); p = d.get("payload") or {}
                    if topic in ("crypto_prices_chainlink", "crypto_prices") and "symbol" in p:
                        append("refprices", {
                            "ts": round(time.time(), 3),
                            "src": "chainlink" if topic.endswith("chainlink") else "binance",
                            "symbol": p.get("symbol"), "value": p.get("value"),
                            "p_ts": p.get("timestamp")})
                        _counts["ref"] += 1
        except Exception:
            await asyncio.sleep(2)


async def task_depth(interval=1.0):
    loop = asyncio.get_event_loop()
    active = {}
    while True:
        now = time.time()
        for a, cfg in ASSETS.items():
            w = active.get(a)
            if not w or now >= w["end"]:
                w2 = await loop.run_in_executor(None, current_window, cfg["prefix"], now)
                if w2:
                    active[a] = w2
        for a in ASSETS:
            w = active.get(a)
            if not w:
                continue
            up = await loop.run_in_executor(None, fetch_book, w["up_id"])
            dn = await loop.run_in_executor(None, fetch_book, w["down_id"])
            append("depth", {"ts": round(now, 3), "asset": a, "slug": w["slug"],
                             "ttc": round(w["end"] - now, 1), "up": up, "dn": dn})
            _counts["depth"] += 1
        await asyncio.sleep(interval)


async def heartbeat():
    while True:
        await asyncio.sleep(30)
        print(f"  {datetime.now(timezone.utc):%H:%M:%S}Z refprices={_counts['ref']} depth={_counts['depth']}",
              flush=True)


async def main():
    print("Recorder v2 starting: Chainlink/Binance refprices + full-depth books. Read-only.", flush=True)
    await asyncio.gather(task_chainlink(), task_depth(), heartbeat())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("stopped.")
