#!/usr/bin/env python3
"""
Estimate return on CAPITAL (not volume) for the top 5-min-market wallets.

For each wallet we rebuild an event stream on the Up leg: every fill's cash flow
(buy = cash out, sell = cash in) plus a synthetic redemption credit at each
window's close (net_shares * payoff). Running cumulative cash gives the capital
they had tied up at each instant; the low point is peak capital deployed.
ROC(3d) = pnl / peak_capital.

Caveats: Up leg only (true capital is higher, so ROC shown is an UPPER bound);
3-day sample; assumes hold-to-resolution (if they flatten intra-window, real
peak is lower). Read-only.
"""
import json, sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import clickhouse_connect

CACHE = "backtest_cache/windows.jsonl"
TARGETS = [
    "0xEEbde7A0E019A63E6b476eb425505b7b3e6EBA30",
    "0x0CB038487586D1119B165466072e9bAf666F3a90",
    "0xB27BC932bf8110D8F78e55da7d5f0497A18B5b82",
    "0xce50C96B976203b53342a0a801067d2cdCfCf46E",
    "0x3048d65321Be3497164cDfc2996F94F98a2e7537",
    "0xaA1A4f31C010d4a63fc49770D4ca2d69Ea370602",
    "0x3BFA6d733cCc74b324D638B38a1ab7bC8f37782a",  # taker-heavy outlier
]


def main():
    ups = {}
    with open(CACHE, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            if "up_token" in o:
                ups[o["up_token"]] = (o["winner_up"], o["start"])
    t0 = min(s for _, s in ups.values()) - 120
    t1 = max(s for _, s in ups.values()) + 600
    pay = ",".join(f"('{t}', {float(w)}, {s})" for t, (w, s) in ups.items())
    tgt = "','".join(TARGETS)

    c = clickhouse_connect.get_client(host="localhost", port=8123,
        username="copypoly", password="copypoly", database="copypoly")
    S = {"max_query_size": 300_000_000, "max_ast_elements": 20_000_000,
         "max_expanded_ast_elements": 20_000_000}

    q = f"""
    WITH
    pay AS (SELECT token, payoff, start_ts FROM
        values('token String, payoff Float64, start_ts UInt32', {pay})),
    legs AS (
      SELECT wallet, token, usdc, ts, signed_cash, shares_signed FROM (
        SELECT if(maker_asset_id='0', maker, taker) AS wallet,
               multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
               toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
               toUInt32(block_timestamp) AS ts,
               -toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS signed_cash,
               toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS shares_signed
        FROM trade_history
        WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
          AND (maker_asset_id IN (SELECT token FROM pay) OR taker_asset_id IN (SELECT token FROM pay))
          AND (maker IN ('{tgt}') OR taker IN ('{tgt}'))
        UNION ALL
        SELECT if(maker_asset_id='0', taker, maker) AS wallet,
               multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
               toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
               toUInt32(block_timestamp) AS ts,
               toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS signed_cash,
               -toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS shares_signed
        FROM trade_history
        WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
          AND (maker_asset_id IN (SELECT token FROM pay) OR taker_asset_id IN (SELECT token FROM pay))
          AND (maker IN ('{tgt}') OR taker IN ('{tgt}'))
      ) WHERE wallet IN ('{tgt}')
    ),
    redem AS (
      SELECT g.wallet AS wallet, (p.start_ts+300) AS ts, g.net*p.payoff AS signed_cash
      FROM (SELECT wallet, token, sum(shares_signed) AS net FROM legs GROUP BY wallet, token) g
      INNER JOIN pay p ON g.token = p.token
    ),
    events AS (
      SELECT wallet, ts, signed_cash FROM legs
      UNION ALL
      SELECT wallet, ts, signed_cash FROM redem
    ),
    runn AS (
      SELECT wallet, sum(signed_cash) OVER
        (PARTITION BY wallet ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS run
      FROM events
    )
    SELECT a.wallet AS wallet,
      round(b.pnl, 0)                          AS pnl,
      round(-a.peak, 0)                        AS peak_capital,
      round(b.pnl / nullIf(-a.peak,0) * 100,0) AS roc_pct_3d,
      cc.fills                                 AS fills,
      round(cc.gross / nullIf(cc.fills,0), 2)  AS avg_trade_usd,
      round(cc.gross, 0)                       AS gross_vol
    FROM (SELECT wallet, min(run) AS peak FROM runn GROUP BY wallet) a
    INNER JOIN (SELECT wallet, sum(signed_cash) AS pnl FROM events GROUP BY wallet) b USING wallet
    INNER JOIN (SELECT wallet, count() AS fills, sum(usdc) AS gross FROM legs GROUP BY wallet) cc USING wallet
    ORDER BY pnl DESC
    """
    rows = c.query(q, settings=S).result_rows
    print(f"{'wallet':<44}{'pnl$':>9}{'peak_cap$':>11}{'ROC/3d':>9}{'fills':>9}{'avg$':>8}")
    for w, pnl, peak, roc, fills, avg, gross in rows:
        print(f"{w:<44}{pnl:>9.0f}{peak:>11.0f}{str(roc)+'%':>9}{fills:>9}{avg:>8.2f}")
    print("\nROC/3d = pnl / peak capital deployed over 3 days (Up leg only -> real capital")
    print("is higher, so treat ROC as an optimistic upper bound). ~x10 = annualized-ish.")


if __name__ == "__main__":
    main()
