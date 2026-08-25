"""Validate the fill-recognition code against REAL data: replay the incident
session's trades (from the exchange's own feed) through Clob.my_fill and
reconstruct per-token positions. Compare against the known Polymarket-UI
positions. READ-ONLY. Run: python -m live.validate_ingest [session_start_unix]"""
from __future__ import annotations

import sys
import datetime

from paper import envload

envload.load()
from live.executor import Clob  # noqa: E402 - env must load before client import


def main():
    t0 = float(sys.argv[1]) if len(sys.argv) > 1 else 1787583257.0   # 14:54:17Z session start
    t1 = float(sys.argv[2]) if len(sys.argv) > 2 else 1787583656.0   # 15:00:56Z kill
    clob = Clob()
    rows, cursor, pages = [], None, 0
    while pages < 40:
        batch = clob.c.get_trades(next_cursor=cursor) if cursor else clob.c.get_trades(only_first_page=True)
        if not batch:
            break
        if isinstance(batch, dict):      # some SDK versions wrap {data, next_cursor}
            cursor = batch.get("next_cursor")
            batch = batch.get("data") or []
        else:
            cursor = None
        rows += batch
        pages += 1
        if not cursor or (batch and float(batch[-1].get("match_time", 0)) < t0 - 300):
            break

    ses = [t for t in rows if t0 <= float(t.get("match_time", 0)) <= t1]
    print(f"feed rows fetched: {len(rows)} | in session {datetime.datetime.fromtimestamp(t0, datetime.UTC):%H:%M:%S}"
          f"-{datetime.datetime.fromtimestamp(t1, datetime.UTC):%H:%M:%S}Z: {len(ses)}")

    pos: dict[str, dict] = {}
    skipped = 0
    for t in ses:
        side, size = clob.my_fill(t)
        if not side or size <= 0:
            skipped += 1
            continue
        k = t.get("asset_id", "?")
        p = pos.setdefault(k, {"out": t.get("outcome"), "sh": 0.0, "usd": 0.0, "buys": 0, "sells": 0})
        px = float(t.get("price", 0))
        if side == "BUY":
            p["sh"] += size
            p["usd"] += size * px
            p["buys"] += 1
        else:
            p["sh"] -= size
            p["usd"] -= size * px
            p["sells"] += 1

    print(f"rows with no recognizable portion of ours (skipped): {skipped}")
    print(f"\n{'token':<14}{'outcome':>8}{'net shares':>12}{'net $':>10}{'buys':>6}{'sells':>7}")
    for k, p in sorted(pos.items(), key=lambda kv: -abs(kv[1]['usd'])):
        print(f"{k[:12]+'..':<14}{str(p['out']):>8}{p['sh']:>12.1f}{p['usd']:>10.2f}{p['buys']:>6}{p['sells']:>7}")
    print("\nEXPECTED from the Polymarket UI (10:55AM + 11:00AM windows):")
    print("  BTC Down ~1183.6 sh (~$182.75) | XRP Down ~1104.6 sh (~$54.90) | BTC Up ~215.0 sh (~$95.00)")
    print("  BTC Up(11:00) ~141.1 | BTC Down(11:00) ~142.1 | XRP Up ~116.6 | ETH Up ~69.0 | ETH Up(11:00) ~47.6")
    print("MATCH = fill-recognition (identity + maker_orders parsing) is proven on real data.")


if __name__ == "__main__":
    main()
