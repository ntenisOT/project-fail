#!/usr/bin/env python3
"""
Find the FULL cohort of wallets that profit on the 5-min crypto markets, segment
them by strategy (maker/taker/mixed) and capital scale, and flag whether they
look like one strategy or many. Read-only except a transient Memory-engine temp
table (pay_cohort) that is dropped at the end.
"""
import json, statistics, sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import clickhouse_connect

CACHE = "backtest_cache/windows.jsonl"
S = {"max_query_size": 300_000_000, "max_ast_elements": 20_000_000,
     "max_expanded_ast_elements": 20_000_000}
MIN_PNL, MIN_FILLS = 2000, 50


def legs_sql(t0, t1):
    """Buyer+seller legs over trades touching the pay_cohort tokens."""
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

    c.command("DROP TABLE IF EXISTS pay_cohort")
    c.command("CREATE TABLE pay_cohort (token String, payoff Float64, start_ts UInt32) ENGINE=Memory")
    c.insert("pay_cohort", [[t, float(w), int(s)] for t, (w, s) in ups.items()],
             column_names=["token", "payoff", "start_ts"])
    print(f"loaded {len(ups)} tokens into pay_cohort")

    try:
        print("Stage 1: per-wallet P&L + style ...")
        q1 = f"""
        SELECT wallet,
          round(sum(cash) + sum(net_sh*payoff),0) AS pnl,
          round(sum(usdc),0) AS gross, count() AS fills,
          round(countIf(is_maker)/count(),3) AS maker_share,
          uniqExact(token) AS markets, round(avg((start_ts+300)-ts),0) AS avg_sc
        FROM (
          SELECT l.wallet AS wallet, l.token AS token, l.usdc AS usdc, l.is_maker AS is_maker,
                 l.signed_cash AS cash, l.shares_signed AS net_sh,
                 p.payoff AS payoff, p.start_ts AS start_ts, l.ts AS ts
          FROM ({legs_sql(t0, t1)}) l
          INNER JOIN pay_cohort p ON l.token = p.token
        )
        GROUP BY wallet HAVING pnl >= {MIN_PNL} AND fills >= {MIN_FILLS}
        ORDER BY pnl DESC
        """
        s1 = c.query(q1, settings=S).result_rows
        cohort = {r[0]: {"pnl": r[1], "gross": r[2], "fills": r[3], "maker": r[4],
                         "markets": r[5], "avg_sc": r[6]} for r in s1}
        print(f"  cohort (pnl>=${MIN_PNL}, fills>={MIN_FILLS}): {len(cohort)} wallets")
        if not cohort:
            return

        print("Stage 2: peak capital / ROC ...")
        wl = "','".join(cohort.keys())
        q2 = f"""
        WITH mine AS (SELECT * FROM ({legs_sql(t0, t1)}) WHERE wallet IN ('{wl}')),
        redem AS (
          SELECT g.wallet AS wallet, (p.start_ts+300) AS ts, g.net*p.payoff AS signed_cash
          FROM (SELECT wallet, token, sum(shares_signed) AS net FROM mine GROUP BY wallet, token) g
          INNER JOIN pay_cohort p ON g.token = p.token
        ),
        events AS (SELECT wallet, ts, signed_cash FROM mine
                   UNION ALL SELECT wallet, ts, signed_cash FROM redem),
        runn AS (SELECT wallet, sum(signed_cash) OVER
            (PARTITION BY wallet ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS run FROM events)
        SELECT wallet, round(-min(run),0) AS peak FROM runn GROUP BY wallet
        """
        for w, peak in c.query(q2, settings=S).result_rows:
            if w in cohort:
                cohort[w]["peak"] = max(peak, 1.0)
                cohort[w]["roc"] = cohort[w]["pnl"] / max(peak, 1.0) * 100
    finally:
        c.command("DROP TABLE IF EXISTS pay_cohort")

    rows = [dict(wallet=w, **v) for w, v in cohort.items() if "roc" in v]
    rows.sort(key=lambda r: r["roc"], reverse=True)

    def seg(r):
        if r["maker"] >= 0.6: return "MAKER"
        if r["maker"] <= 0.2: return "TAKER"
        return "MIXED"

    print(f"\n=== COHORT: {len(rows)} profitable wallets on 5-min markets (3d sample) ===")
    for label in ["MAKER", "MIXED", "TAKER"]:
        g = [r for r in rows if seg(r) == label]
        if not g:
            continue
        print(f"\n{label}: {len(g)} wallets | total pnl ${sum(r['pnl'] for r in g):,.0f} | "
              f"median ROC {statistics.median(r['roc'] for r in g):.0f}%/3d | "
              f"median cap ${statistics.median(r['peak'] for r in g):,.0f} | "
              f"median fills {statistics.median(r['fills'] for r in g):,.0f} | "
              f"median entry {statistics.median(r['avg_sc'] for r in g):.0f}s-to-close")

    hi = [r for r in rows if r["roc"] >= 500 and r["peak"] <= 20000]
    print(f"\nHIGH-ROC + SMALL-CAP (ROC>=500%/3d & cap<=$20k): {len(hi)} wallets")
    print(f"\n{'wallet':<44}{'seg':>6}{'pnl$':>9}{'cap$':>9}{'ROC%':>8}{'fills':>8}{'mkr':>6}{'mkts':>6}{'s2c':>6}")
    for r in rows[:30]:
        print(f"{r['wallet']:<44}{seg(r):>6}{r['pnl']:>9.0f}{r['peak']:>9.0f}"
              f"{r['roc']:>8.0f}{r['fills']:>8}{r['maker']*100:>5.0f}%{r['markets']:>6}{r['avg_sc']:>6.0f}")


if __name__ == "__main__":
    main()
