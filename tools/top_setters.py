#!/usr/bin/env python3
"""Top set-trader wallets over today's resolved windows: per-wallet PnL plus
the two set-strategy fingerprints - both-sides share (fraction of active
windows where the wallet traded BOTH outcome tokens) and minted share
(shares sold beyond book-bought = inventory that had to come from
splitPosition). Read-only against local ClickHouse."""
import json
import sys

import clickhouse_connect

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SCRATCH = (r"C:/Users/nteni/AppData/Local/Temp/claude/C--Users-nteni-project-fail"
           r"/2b043fb0-a745-4981-932a-af2f44973415/scratchpad/outcomes.json")

outcomes = json.load(open(SCRATCH, encoding="utf-8"))
cache = json.load(open("backtest_cache/slug_tokens.json", encoding="utf-8"))
rows = []
for slug, out in outcomes.items():
    ids = cache.get(slug)
    if not ids:
        continue
    base = int(slug.rsplit("-", 1)[-1])
    rows.append([ids[0], float(out), base])
    rows.append([ids[1], float(1 - out), base])
print(f"windows with outcome+tokens: {len(rows)//2}")
t0 = min(r[2] for r in rows) - 30
t1 = max(r[2] for r in rows) + 330

c = clickhouse_connect.get_client(host="localhost", port=8123,
    username="copypoly", password="copypoly", database="copypoly")
S = {"max_query_size": 300_000_000, "enable_analyzer": 1}
c.command("DROP TABLE IF EXISTS pay_t")
c.command("CREATE TABLE pay_t (token String, payoff Float64, base UInt32) ENGINE=Memory")
c.insert("pay_t", rows, column_names=["token", "payoff", "base"])

LEGS = f"""
  SELECT if(maker_asset_id='0', maker, taker) AS wallet,
         multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
         toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
         -toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS cash,
         toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS net_sh,
         toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS bought_sh,
         0.0 AS sold_sh
  FROM trade_history
  WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
    AND (maker_asset_id IN (SELECT token FROM pay_t) OR taker_asset_id IN (SELECT token FROM pay_t))
  UNION ALL
  SELECT if(maker_asset_id='0', taker, maker) AS wallet,
         multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
         toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
         toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS cash,
         -toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS net_sh,
         0.0 AS bought_sh,
         toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS sold_sh
  FROM trade_history
  WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
    AND (maker_asset_id IN (SELECT token FROM pay_t) OR taker_asset_id IN (SELECT token FROM pay_t))
"""
q = f"""
WITH per_tok AS (
  SELECT wallet, L.token AS token, any(p.base) AS base,
         sum(cash) + sum(net_sh * p.payoff) AS pnl,
         sum(usdc) AS vol, sum(bought_sh) AS bought, sum(sold_sh) AS sold
  FROM ({LEGS}) L INNER JOIN pay_t p ON L.token = p.token
  GROUP BY wallet, L.token
), per_win AS (
  SELECT wallet, base, count() AS sides, sum(pnl) AS pnl, sum(vol) AS vol,
         sum(bought) AS bought, sum(sold) AS sold
  FROM per_tok GROUP BY wallet, base
)
SELECT wallet, round(sum(pnl), 0) AS pnl, round(sum(vol), 0) AS vol,
       count() AS windows, round(avg(sides = 2) * 100, 0) AS both_pct,
       round(100 * sum(greatest(0.0, sold - bought)) / greatest(1.0, sum(sold)), 0) AS minted_pct
FROM per_win
GROUP BY wallet
HAVING windows >= 20 AND both_pct >= 50
ORDER BY pnl DESC LIMIT 10
"""
print(f"{'wallet':<44}{'pnl$':>9}{'vol$':>11}{'windows':>8}{'both%':>7}{'minted%':>8}")
for w, pnl, vol, wins, both, minted in c.query(q, settings=S).result_rows:
    print(f"{w:<44}{pnl:>+9,.0f}{vol:>11,.0f}{wins:>8}{both:>6.0f}%{minted:>7.0f}%")
c.command("DROP TABLE IF EXISTS pay_t")
