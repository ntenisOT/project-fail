#!/usr/bin/env python3
"""Quantify the near-close EXIT liquidity: how much $ trades at >=0.95 in the final
30 seconds of a window (i.e. how much of a winning position you could actually sell
near $1 instead of holding through the ~2h redemption). Read-only."""
import json, statistics, sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import clickhouse_connect

S = {"max_query_size": 300_000_000, "max_ast_elements": 20_000_000, "max_expanded_ast_elements": 20_000_000}


def main():
    ups = {}
    for line in open("backtest_cache/windows.jsonl", encoding="utf-8"):
        o = json.loads(line)
        if "up_token" in o:
            ups[o["up_token"]] = o["start"]
    t0 = min(ups.values()) - 60
    t1 = max(ups.values()) + 360
    c = clickhouse_connect.get_client(host="localhost", port=8123,
        username="copypoly", password="copypoly", database="copypoly")
    c.command("DROP TABLE IF EXISTS pay_l")
    c.command("CREATE TABLE pay_l (token String, start_ts UInt32) ENGINE=Memory")
    c.insert("pay_l", [[t, int(s)] for t, s in ups.items()], column_names=["token", "start_ts"])
    try:
        q = f"""
        SELECT token, round(sum(usdc),2) AS exit_usd, count() AS n, round(avg(price),4) AS avg_px
        FROM (
          SELECT tr.token AS token,
                 (w.start_ts+300)-tr.ts AS sc, tr.price AS price, tr.usdc AS usdc
          FROM (
            SELECT multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
                   toUInt32(block_timestamp) AS ts,
                   toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
                   toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS toks,
                   if(toks>0, toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled)), 0) AS price
            FROM trade_history
            WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
              AND (maker_asset_id IN (SELECT token FROM pay_l) OR taker_asset_id IN (SELECT token FROM pay_l))
          ) tr INNER JOIN pay_l w ON tr.token = w.token
        )
        WHERE sc BETWEEN 0 AND 30 AND price >= 0.95
        GROUP BY token
        """
        rows = c.query(q, settings=S).result_rows
    finally:
        c.command("DROP TABLE IF EXISTS pay_l")

    vols = [r[1] for r in rows]
    pxs = [r[3] for r in rows]
    n_win = len(ups)
    print(f"windows sampled: {n_win}")
    print(f"windows with >=0.95 volume in last 30s (i.e. a winner with exit liquidity): {len(rows)} ({100*len(rows)/n_win:.0f}%)")
    if vols:
        vols.sort()
        def p(q): return vols[min(len(vols)-1, int(q*len(vols)))]
        print(f"exit $ available in last 30s @>=0.95 — median ${p(0.5):,.0f} | p25 ${p(0.25):,.0f} | p75 ${p(0.75):,.0f} | p90 ${p(0.9):,.0f} | max ${max(vols):,.0f}")
        print(f"avg exit price in that zone: {statistics.fmean(pxs):.4f}  (haircut vs $1 = {(1-statistics.fmean(pxs))*100:.2f}%)")


if __name__ == "__main__":
    main()
