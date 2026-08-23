#!/usr/bin/env python3
"""
Profile the winning wallets' actual behaviour on the 5-min markets to understand
the strategy: is their profit SPREAD capture (market-making) or DIRECTIONAL
(holding to resolution)? Do they end each window flat? How big/where do they quote?

Decomposition per window: pnl = spread_pnl (net trading cashflow) + settle_pnl
(net shares held * payoff). Flat MM => spread_pnl dominates, ends flat.
Read-only (transient Memory table pay_cohort).
"""
import json, statistics, sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import clickhouse_connect

CACHE = "backtest_cache/windows.jsonl"
S = {"max_query_size": 300_000_000, "max_ast_elements": 20_000_000,
     "max_expanded_ast_elements": 20_000_000, "enable_analyzer": 1}
TARGETS = [
    ("0x5195a3D459A40e6503ccF19798f3CD9551B0d864", "MAKER"),
    ("0x7Cf89836b082aAB85B137d45bFC6B991Cf2104Ae", "MAKER"),
    ("0xA62ff3d3d2fe4456523c567441Acb512f0BA9E04", "MAKER"),
    ("0x3048d65321Be3497164cDfc2996F94F98a2e7537", "MIXED"),
    ("0xcE25E214D5cfE4f459cf67F08DF581885AAE7Fdc", "MIXED"),
    ("0xc53375Ff94E96100f2B30A4b5775DB35218d69a9", "MIXED"),
    ("0x32ed2e546b187CA15e2841edC82b22C713cf8eC3", "MIXED"),
    ("0x3BFA6d733cCc74b324D638B38a1ab7bC8f37782a", "TAKER"),
    ("0x391FF4c0F1183C787407dfE399E36EfcAFA4d9dA", "TAKER"),
    ("0x4E6F446878259Fb3d5e536F711980Ac63aa0358E", "TAKER"),
]


def legs_sql(t0, t1):
    return f"""
      SELECT if(maker_asset_id='0', maker, taker) AS wallet,
             multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
             toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
             toUInt32(block_timestamp) AS ts,
             -toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS signed_cash,
             toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS shares_signed,
             maker_asset_id='0' AS is_maker
      FROM trade_history
      WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
        AND (maker IN (SELECT wallet FROM tgt) OR taker IN (SELECT wallet FROM tgt))
        AND (maker_asset_id IN (SELECT token FROM pay_cohort) OR taker_asset_id IN (SELECT token FROM pay_cohort))
      UNION ALL
      SELECT if(maker_asset_id='0', taker, maker) AS wallet,
             multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
             toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
             toUInt32(block_timestamp) AS ts,
             toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS signed_cash,
             -toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS shares_signed,
             maker_asset_id!='0' AS is_maker
      FROM trade_history
      WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
        AND (maker IN (SELECT wallet FROM tgt) OR taker IN (SELECT wallet FROM tgt))
        AND (maker_asset_id IN (SELECT token FROM pay_cohort) OR taker_asset_id IN (SELECT token FROM pay_cohort))"""


def main():
    ups = {}
    with open(CACHE, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            if "up_token" in o:
                ups[o["up_token"]] = (o["winner_up"], o["start"])
    t0 = min(s for _, s in ups.values()) - 120
    t1 = max(s for _, s in ups.values()) + 600

    c = clickhouse_connect.get_client(host="localhost", port=8123,
        username="copypoly", password="copypoly", database="copypoly")
    seg = {w: s for w, s in TARGETS}
    for name, ddl in [("pay_cohort", "token String, payoff Float64, start_ts UInt32"),
                      ("tgt", "wallet String")]:
        c.command(f"DROP TABLE IF EXISTS {name}")
        c.command(f"CREATE TABLE {name} ({ddl}) ENGINE=Memory")
    c.insert("pay_cohort", [[t, float(w), int(s)] for t, (w, s) in ups.items()],
             column_names=["token", "payoff", "start_ts"])
    c.insert("tgt", [[w] for w, _ in TARGETS], column_names=["wallet"])

    try:
        qA = f"""
        SELECT wallet,
          round(sum(cash),0) AS spread_pnl,
          round(sum(net_sh*payoff),0) AS settle_pnl,
          round(sum(cash)+sum(net_sh*payoff),0) AS pnl,
          count() AS fills, round(countIf(is_maker)/count(),2) AS maker_share,
          round(sum(usdc)/count(),2) AS avg_trade, round(avg(sc),0) AS avg_sc
        FROM (
          SELECT l.wallet AS wallet, l.signed_cash AS cash, l.shares_signed AS net_sh,
                 p.payoff AS payoff, l.usdc AS usdc, l.is_maker AS is_maker, (p.start_ts+300)-l.ts AS sc
          FROM ({legs_sql(t0, t1)}) l INNER JOIN pay_cohort p ON l.token=p.token
        ) GROUP BY wallet
        """
        qB = f"""
        SELECT wallet,
          round(countIf(gross>0 AND abs(net)/gross < 0.15)/count(),2) AS flat_rate,
          round(avg(if(gross>0, abs(net)/gross, 0)),2) AS inv_ratio, count() AS windows
        FROM (
          SELECT wallet, token, sum(shares_signed) AS net, sum(abs(shares_signed)) AS gross
          FROM ({legs_sql(t0, t1)}) l GROUP BY wallet, token
        ) GROUP BY wallet
        """
        A = {r[0]: r for r in c.query(qA, settings=S).result_rows}
        B = {r[0]: r for r in c.query(qB, settings=S).result_rows}
        rows = {}
        for w in A:
            a = A[w]; b = B.get(w, (w, 0, 0, 0))
            # (wallet, spread, settle, pnl, flat, invr, fills, mkr, avgt, sc, windows)
            rows[w] = (w, a[1], a[2], a[3], b[1], b[2], a[4], a[5], a[6], a[7], b[3])
    finally:
        for name in ["pay_cohort", "tgt"]:
            c.command(f"DROP TABLE IF EXISTS {name}")

    print(f"{'wallet':<16}{'seg':>6}{'pnl$':>8}{'spread$':>9}{'settle$':>9}{'flat%':>7}{'invR':>6}{'fills':>8}{'mkr':>6}{'avg$':>7}{'s2c':>6}")
    for w, s in TARGETS:
        r = rows.get(w)
        if not r:
            print(f"{w[:14]:<16}{s:>6}  (no data)"); continue
        _, spread, settle, pnl, flat, invr, fills, mkr, avgt, sc, wins = r
        print(f"{w[:14]:<16}{s:>6}{pnl:>8.0f}{spread:>9.0f}{settle:>9.0f}"
              f"{flat*100:>6.0f}%{invr:>6.2f}{fills:>8}{mkr*100:>5.0f}%{avgt:>7.2f}{sc:>6.0f}")
    print("\nspread$ = profit from trading in/out (market-making); settle$ = profit from")
    print("holding to resolution (directional). flat% = windows ending ~flat. invR =")
    print("avg leftover inventory ratio (0=always offloads, 1=never). s2c=avg entry secs-to-close.")


if __name__ == "__main__":
    main()
