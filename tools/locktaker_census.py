#!/usr/bin/env python3
"""Who actually takes locks on the 5m up/down books? A lock-taker's on-chain
fingerprint: the SAME wallet buys BOTH tokens of a window's pair in the SAME
block (Polygon ~2s). Counts wallets/events/sizes over the cached windows.
Read-only against local ClickHouse."""
import json
import sys

import clickhouse_connect

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

cache = json.load(open("backtest_cache/slug_tokens.json", encoding="utf-8"))
pairs = [(v[0], v[1]) for v in cache.values() if v and len(v) >= 2]
print(f"windows with token pairs cached: {len(pairs)}")
if not pairs:
    sys.exit(0)

c = clickhouse_connect.get_client(host="localhost", port=8123,
    username="copypoly", password="copypoly", database="copypoly")
S = {"max_query_size": 300_000_000, "enable_analyzer": 1}

c.command("DROP TABLE IF EXISTS pair_census")
c.command("CREATE TABLE pair_census (up String, dn String) ENGINE=Memory")
c.insert("pair_census", pairs, column_names=["up", "dn"])

# BUY legs only: the wallet RECEIVES the conditional token (pays USDC).
# leg = (wallet, token, block, usdc). Join Up-leg x Dn-leg of the same pair,
# same wallet, same block -> lock-take event.
q = """
WITH legs AS (
    SELECT if(maker_asset_id='0', maker, taker)                    AS wallet,
           multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
           block_timestamp                                          AS blk,
           toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc
    FROM trade_history
    WHERE block_timestamp >= toDateTime(1787553900)
      AND block_timestamp <  toDateTime(1787603700)
      AND (maker_asset_id='0' OR taker_asset_id='0')
)
SELECT count() AS events, uniqExact(u.wallet) AS wallets,
       round(sum(u.usdc + d.usdc), 0) AS usd_total,
       round(avg(u.usdc + d.usdc), 2) AS usd_avg,
       min(u.blk) AS first_seen, max(u.blk) AS last_seen
FROM legs u
INNER JOIN pair_census p ON u.token = p.up
INNER JOIN legs d ON d.token = p.dn AND d.wallet = u.wallet AND d.blk = u.blk
"""
r = c.query(q, settings=S).result_rows[0]
events, wallets, usd_total, usd_avg, first_seen, last_seen = r
print(f"SAME-BLOCK both-sides buys (lock-take fingerprint):")
print(f"  events {events} | wallets {wallets} | total ${usd_total:,.0f} | avg ${usd_avg}/event")
print(f"  span: {first_seen} .. {last_seen}")

if events:
    q2 = """
    WITH legs AS (
        SELECT if(maker_asset_id='0', maker, taker) AS wallet,
               multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
               block_timestamp AS blk,
               toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc
        FROM trade_history
        WHERE maker_asset_id='0' OR taker_asset_id='0'
    )
    SELECT u.wallet, count() AS n, round(sum(u.usdc + d.usdc), 0) AS usd
    FROM legs u
    INNER JOIN pair_census p ON u.token = p.up
    INNER JOIN legs d ON d.token = p.dn AND d.wallet = u.wallet AND d.blk = u.blk
    GROUP BY u.wallet ORDER BY n DESC LIMIT 8
    """
    print("top lock-takers:")
    for w, n, usd in c.query(q2, settings=S).result_rows:
        print(f"  {w[:10]}...  {n:5d} events  ${usd:,.0f}")
c.command("DROP TABLE IF EXISTS pair_census")
