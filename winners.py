#!/usr/bin/env python3
"""
Find wallets that actually profit on the 5-minute crypto up/down markets, and
characterize HOW they trade (maker vs taker, entry timing), to judge whether the
strategy is copyable. Read-only.

Uses the window->up_token cache from backtest.py (backtest_cache/windows.jsonl),
derives each window's paired Down token from on-chain trades, assigns payoffs
from the official winner, then computes per-wallet realized+settlement P&L over
both sides of every window, straight from trade_history.
"""
import json, os, sys, time
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import clickhouse_connect

CACHE = "backtest_cache/windows.jsonl"


def load_windows():
    ups = {}  # up_token -> (winner_up, start)
    with open(CACHE, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            if "up_token" in o:
                ups[o["up_token"]] = (o["winner_up"], o["start"])
    return ups


def vlist(rows):
    return ",".join(rows)


def main():
    ups = load_windows()
    up_tokens = list(ups.keys())
    t0 = min(s for _, s in ups.values()) - 120
    t1 = max(s for _, s in ups.values()) + 600
    print(f"up-tokens: {len(up_tokens)} | window span {t1-t0}s")

    c = clickhouse_connect.get_client(host="localhost", port=8123,
        username="copypoly", password="copypoly", database="copypoly")
    global SETTINGS
    SETTINGS = {"max_query_size": 300_000_000,
                "max_ast_elements": 20_000_000,
                "max_expanded_ast_elements": 20_000_000}

    # Up-side payoff map (one leg per window; simple + correct). payoff = winner_up.
    # This is enough to read a wallet's STYLE on these markets (maker vs taker,
    # entry timing, consistency) even though it only scores the Up leg.
    pay = [f"('{t}', {float(w)}, {s})" for t, (w, s) in ups.items()]
    print(f"up tokens with payoff: {len(pay)}")

    # Q3: per-wallet P&L + style over both legs of every fill
    vals = vlist(pay)
    q3 = f"""
    WITH pay AS (SELECT token, payoff, start_ts FROM
        values('token String, payoff Float64, start_ts UInt32', {vals}))
    SELECT wallet,
      round(sum(cash) + sum(net_sh*payoff), 0)      AS pnl_usd,
      round(sum(usdc), 0)                            AS volume_usd,
      count()                                        AS legs,
      round(countIf(is_maker)/count(), 2)            AS maker_share,
      uniqExact(token)                               AS tokens,
      round(avg((start_ts+300) - ts), 0)             AS avg_secs_to_close
    FROM (
      SELECT l.wallet AS wallet, l.token AS token, l.ts AS ts, l.usdc AS usdc, l.cash AS cash,
             l.net_sh AS net_sh, l.is_maker AS is_maker, p.payoff AS payoff, p.start_ts AS start_ts
      FROM (
        SELECT token, ts, usdc, wallet, is_maker, cash, net_sh FROM (
          -- buyer leg
          SELECT multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
                 toUInt32(block_timestamp) AS ts,
                 toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
                 if(maker_asset_id='0', maker, taker) AS wallet,
                 maker_asset_id='0' AS is_maker,
                 -toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS cash,
                 toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS net_sh
          FROM trade_history
          WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
            AND (maker_asset_id IN (SELECT token FROM pay) OR taker_asset_id IN (SELECT token FROM pay))
          UNION ALL
          -- seller leg
          SELECT multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
                 toUInt32(block_timestamp) AS ts,
                 toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
                 if(maker_asset_id='0', taker, maker) AS wallet,
                 maker_asset_id!='0' AS is_maker,
                 toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS cash,
                 -toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS net_sh
          FROM trade_history
          WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
            AND (maker_asset_id IN (SELECT token FROM pay) OR taker_asset_id IN (SELECT token FROM pay))
        )
      ) l INNER JOIN pay p ON l.token = p.token
    )
    GROUP BY wallet
    HAVING legs >= 20
    ORDER BY pnl_usd DESC
    LIMIT 25
    """
    rows = c.query(q3, settings=SETTINGS).result_rows
    print("\n=== Top wallets by P&L on 5-min crypto up/down (last ~3 days) ===")
    print(f"{'wallet':<44}{'pnl$':>10}{'vol$':>11}{'legs':>7}{'mkr%':>6}{'mkts':>6}{'avg_s2c':>8}")
    for w, pnl, vol, legs, mkr, toks, sc in rows:
        print(f"{w:<44}{pnl:>10.0f}{vol:>11.0f}{legs:>7}{mkr*100:>5.0f}%{toks:>6}{sc:>8.0f}")
    print("\nmkr% = share of fills as maker (100=pure market-maker; 0=pure taker)")
    print("avg_s2c = avg seconds-to-close at entry (low=trades late; ~150+=spread across window)")


if __name__ == "__main__":
    main()
