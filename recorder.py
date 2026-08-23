#!/usr/bin/env python3
"""
Polymarket 5-minute crypto up/down RECORDER  (read-only, no wallet, no trading)

For BTC / ETH / SOL / XRP it collects, once or twice per second:
  - the Polymarket Up and Down token order books (best bid/ask + mid)
  - a reference underlying spot price (proxy for the Chainlink settlement feed)
  - seconds remaining until the 5-minute window closes
and, after each window ends, the OFFICIAL resolved outcome (Up or Down).

Everything is written as newline-delimited JSON under ./out/ so you can later
test whether any entry rule (spike-follow, mid-probability, last-20s-when-close)
or market-making rule would have made money AFTER fees.

It NEVER places an order, needs no login, and never sees a wallet or key.
It only reads public endpoints.

Usage:
    python recorder.py                 # run forever (Ctrl-C to stop)
    python recorder.py --duration 60   # run 60s (smoke test)
    python recorder.py --assets btc,eth
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ASSETS = {
    "btc": {"prefix": "btc-updown-5m", "spot": "BTCUSDT", "name": "Bitcoin"},
    "eth": {"prefix": "eth-updown-5m", "spot": "ETHUSDT", "name": "Ethereum"},
    "sol": {"prefix": "sol-updown-5m", "spot": "SOLUSDT", "name": "Solana"},
    "xrp": {"prefix": "xrp-updown-5m", "spot": "XRPUSDT", "name": "XRP"},
}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def http_json(url, data=None, timeout=15, retries=1):
    """GET/POST JSON with a single retry. Raises on final failure."""
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    last = None
    for _ in range(retries + 1):
        try:
            if data is not None:
                body = json.dumps(data).encode()
                req = urllib.request.Request(
                    url, data=body, headers={**headers, "Content-Type": "application/json"}
                )
            else:
                req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - network layer, keep looping
            last = e
            time.sleep(0.3)
    raise last


def spot_prices(symbols):
    """Return ({symbol: price}, source). Binance batch first, Coinbase fallback."""
    try:
        q = '["' + '","'.join(symbols) + '"]'
        url = f"https://api.binance.com/api/v3/ticker/price?symbols={urllib.parse.quote(q)}"
        arr = http_json(url)
        return {d["symbol"]: float(d["price"]) for d in arr}, "binance"
    except Exception:  # noqa: BLE001
        pass
    out = {}
    for s in symbols:
        base = s.replace("USDT", "")
        try:
            d = http_json(f"https://api.coinbase.com/v2/prices/{base}-USD/spot")
            out[s] = float(d["data"]["amount"])
        except Exception:  # noqa: BLE001
            out[s] = None
    return out, "coinbase"


def book_bba(token_id):
    """Return (best_bid, best_ask, mid) for a token, or (None, None, None)."""
    try:
        b = http_json(f"{CLOB}/book?token_id={token_id}")
        bids = b.get("bids") or []
        asks = b.get("asks") or []
        bb = max((float(x["price"]) for x in bids), default=None)
        ba = min((float(x["price"]) for x in asks), default=None)
        mid = (bb + ba) / 2 if (bb is not None and ba is not None) else None
        return bb, ba, mid
    except Exception:  # noqa: BLE001
        return None, None, None


def current_window(prefix, now):
    """Discover the currently-open 5-minute market for an asset via deterministic slug."""
    base = int(now - (now % 300))
    slug = f"{prefix}-{base}"
    try:
        ev = http_json(f"{GAMMA}/events?slug={slug}")
    except Exception:  # noqa: BLE001
        return None
    if not ev:
        return None
    e = ev[0]
    mkts = e.get("markets") or []
    if not mkts:
        return None
    m = mkts[0]
    ids = m.get("clobTokenIds")
    if isinstance(ids, str):
        ids = json.loads(ids)
    if not ids or len(ids) < 2:
        return None
    end_epoch = base + 300
    try:
        end_epoch = int(
            datetime.fromisoformat(e["endDate"].replace("Z", "+00:00")).timestamp()
        )
    except Exception:  # noqa: BLE001
        pass
    return {
        "slug": slug,
        "start": base,
        "end": end_epoch,
        "up_id": ids[0],
        "down_id": ids[1],
        "condition_id": m.get("conditionId"),
        "title": e.get("title"),
    }


def resolve_window(prefix, start):
    """Return 'Up'/'Down' once the window has officially settled, else None."""
    slug = f"{prefix}-{start}"
    try:
        ev = http_json(f"{GAMMA}/events?slug={slug}")
    except Exception:  # noqa: BLE001
        return None
    if not ev:
        return None
    m = (ev[0].get("markets") or [{}])[0]
    op = m.get("outcomePrices")
    if isinstance(op, str):
        op = json.loads(op)
    if op and m.get("closed"):
        return "Up" if float(op[0]) > 0.5 else "Down"
    return None


class Out:
    def __init__(self, outdir):
        self.dir = outdir
        os.makedirs(outdir, exist_ok=True)

    def append(self, fn, obj):
        with open(os.path.join(self.dir, fn), "a", encoding="utf-8") as f:
            f.write(json.dumps(obj) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default="btc,eth,sol,xrp")
    ap.add_argument("--out", default="out")
    ap.add_argument("--duration", type=float, default=0.0, help="seconds; 0 = forever")
    ap.add_argument("--fast-interval", type=float, default=1.0, help="sample gap in final minute")
    ap.add_argument("--slow-interval", type=float, default=2.0, help="sample gap otherwise")
    ap.add_argument("--fast-window", type=float, default=60.0, help="ttc under which fast interval kicks in")
    args = ap.parse_args()

    assets = [a.strip() for a in args.assets.split(",") if a.strip() in ASSETS]
    if not assets:
        print("No valid assets. Choose from:", ", ".join(ASSETS))
        sys.exit(1)

    out = Out(args.out)
    tick_file = f"ticks-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    started = time.time()
    print(f"Recorder starting | assets={assets} | out={args.out}/ | tick_file={tick_file}")
    print("Read-only. No orders, no wallet. Ctrl-C to stop.\n")

    active = {}      # asset -> window dict
    wstate = {}      # asset -> {start_spot,start_ts,last_spot,start_up_mid,last_up_mid}
    pending = []     # windows awaiting official resolution
    last_hb = 0.0

    def discover(a):
        w = current_window(ASSETS[a]["prefix"], time.time())
        if w:
            active[a] = w
            wstate[a] = {
                "start_spot": None, "start_ts": None, "last_spot": None,
                "start_up_mid": None, "last_up_mid": None,
            }
        return w

    # startup self-test
    for a in assets:
        w = discover(a)
        print(f"  [{a}] {'OK  ' + w['title'] if w else 'no active window yet'}")
    print()

    try:
        while True:
            now = time.time()
            if args.duration and now - started >= args.duration:
                break

            # roll finished windows into pending, rediscover
            for a in assets:
                w = active.get(a)
                if w and now >= w["end"]:
                    st = wstate.get(a, {})
                    pending.append({
                        "asset": a, "prefix": ASSETS[a]["prefix"], "name": ASSETS[a]["name"],
                        "slug": w["slug"], "start": w["start"], "end": w["end"],
                        "start_spot": st.get("start_spot"), "end_spot": st.get("last_spot"),
                        "start_up_mid": st.get("start_up_mid"), "end_up_mid": st.get("last_up_mid"),
                        "tries": 0,
                    })
                    active[a] = None
                if not active.get(a):
                    discover(a)

            # adaptive cadence: fast if any window is near close
            ttcs = [active[a]["end"] - now for a in assets if active.get(a)]
            interval = args.fast_interval if (ttcs and min(ttcs) <= args.fast_window) else args.slow_interval

            # one spot batch for all requested assets
            symbols = [ASSETS[a]["spot"] for a in assets]
            spots, src = spot_prices(symbols)

            for a in assets:
                w = active.get(a)
                if not w:
                    continue
                spot = spots.get(ASSETS[a]["spot"])
                up_bid, up_ask, up_mid = book_bba(w["up_id"])
                dn_bid, dn_ask, dn_mid = book_bba(w["down_id"])
                out.append(tick_file, {
                    "ts": round(now, 3),
                    "iso": datetime.now(timezone.utc).isoformat(),
                    "asset": a,
                    "slug": w["slug"],
                    "ttc": round(w["end"] - now, 2),
                    "spot": spot,
                    "spot_src": src,
                    "up_bid": up_bid, "up_ask": up_ask, "up_mid": up_mid,
                    "dn_bid": dn_bid, "dn_ask": dn_ask, "dn_mid": dn_mid,
                })
                st = wstate.setdefault(a, {})
                if st.get("start_spot") is None and spot is not None:
                    st["start_spot"] = spot
                    st["start_ts"] = now
                    st["start_up_mid"] = up_mid
                if spot is not None:
                    st["last_spot"] = spot
                if up_mid is not None:
                    st["last_up_mid"] = up_mid

            # resolve pending windows a few seconds after close
            still = []
            for p in pending:
                if now < p["end"] + 6:
                    still.append(p)
                    continue
                outcome = resolve_window(p["prefix"], p["start"])
                if outcome:
                    move = None
                    if p["start_spot"] and p["end_spot"]:
                        move = round((p["end_spot"] - p["start_spot"]) / p["start_spot"] * 100, 4)
                    out.append("resolutions.jsonl", {
                        "asset": p["asset"], "slug": p["slug"],
                        "start": p["start"], "end": p["end"],
                        "outcome": outcome,
                        "start_spot": p["start_spot"], "end_spot": p["end_spot"],
                        "spot_move_pct": move,
                        "start_up_mid": p["start_up_mid"], "end_up_mid": p["end_up_mid"],
                        "resolved_iso": datetime.now(timezone.utc).isoformat(),
                    })
                else:
                    p["tries"] += 1
                    if p["tries"] < 25:
                        still.append(p)
            pending = still

            # heartbeat
            if now - last_hb >= 15:
                last_hb = now
                bits = []
                for a in assets:
                    w = active.get(a)
                    if w:
                        st = wstate.get(a, {})
                        um = st.get("last_up_mid")
                        bits.append(f"{a}:up={um if um is not None else '-'}@{int(w['end']-now)}s")
                print(f"  {datetime.now(timezone.utc):%H:%M:%S}Z [{src}] " + " | ".join(bits)
                      + f" | pending={len(pending)}")

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\nStopped by user.")
    print(f"Done. Data in {args.out}/  (ticks + resolutions.jsonl)")


if __name__ == "__main__":
    main()
