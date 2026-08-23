#!/usr/bin/env python3
"""How do the winning wallets size and manage risk? Read straight from trade_history.

Per (wallet, token) over the last 12h: buy notional (deployment), sell notional,
shares, and avg buy/sell price -> answers:
  1. sizing: distribution of deployment per window-side (is there a cap?)
  2. hold vs flatten: sell/buy ratio (0 = hold to settlement, ~1 = fully exit)
  3. stop-loss: among positions they exit, do they sell BELOW their buy (cut loser)
     or ABOVE (spread capture)?
Read-only.
"""
import statistics
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import clickhouse_connect

TARGETS = [
    "0xB27BC932bf8110D8F78e55da7d5f0497A18B5b82",
    "0xEEbde7A0E019A63E6b476eb425505b7b3e6EBA30",
    "0x3048d65321Be3497164cDfc2996F94F98a2e7537",
    "0x0CB038487586D1119B165466072e9bAf666F3a90",
    "0xcE25E214D5cfE4f459cf67F08DF581885AAE7Fdc",
    "0xc53375Ff94E96100f2B30A4b5775DB35218d69a9",
    "0x5195a3D459A40e6503ccF19798f3CD9551B0d864",
    "0x20d2309Cd92B797aE7ca175ED828ED8a27fbe29D",
]


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(len(xs) - 1, int(p / 100 * len(xs)))
    return xs[i]


def main():
    c = clickhouse_connect.get_client(host="localhost", port=8123,
        username="copypoly", password="copypoly", database="copypoly")
    T = "','".join(TARGETS)
    q = f"""
    SELECT wallet, token,
      sum(if(is_buy, usdc, 0)) AS buy_usdc, sum(if(is_buy=0, usdc, 0)) AS sell_usdc,
      sum(if(is_buy, sh, 0))   AS buy_sh,   sum(if(is_buy=0, sh, 0))   AS sell_sh
    FROM (
      SELECT if(maker_asset_id='0', maker, taker) AS wallet,
             multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
             toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
             toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS sh, 1 AS is_buy
      FROM trade_history
      WHERE block_timestamp >= now()-INTERVAL 12 HOUR AND (maker IN ('{T}') OR taker IN ('{T}'))
      UNION ALL
      SELECT if(maker_asset_id='0', taker, maker) AS wallet,
             multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
             toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
             toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS sh, 0 AS is_buy
      FROM trade_history
      WHERE block_timestamp >= now()-INTERVAL 12 HOUR AND (maker IN ('{T}') OR taker IN ('{T}'))
    )
    WHERE wallet IN ('{T}')
    GROUP BY wallet, token
    """
    rows = c.query(q).result_rows
    by = {}
    for wallet, token, bu, su, bsh, ssh in rows:
        by.setdefault(wallet, []).append((bu, su, bsh, ssh))
    print(f"{'wallet':<14}{'bets':>6}{'dep_med':>9}{'dep_p90':>9}{'dep_p99':>9}{'dep_max':>9}"
          f"{'sell/buy':>9}{'exit%':>7}{'cutloss%':>9}")
    for w in TARGETS:
        recs = by.get(w, [])
        if not recs:
            print(f"{w[:12]:<14}  (no trades in 12h)"); continue
        deps = [r[0] for r in recs if r[0] > 0]                       # buy notional per bet
        srat = statistics.median([r[1] / r[0] for r in recs if r[0] > 0])  # sell/buy
        exited = [r for r in recs if r[0] > 0 and r[3] > 0.05 * r[2]]      # sold >5% of shares bought
        cut = 0
        for bu, su, bsh, ssh in exited:
            if bsh > 0 and ssh > 0 and (su / ssh) < (bu / bsh):          # avg sell < avg buy = loss
                cut += 1
        print(f"{w[:12]:<14}{len(recs):>6}{pct(deps,50):>9.2f}{pct(deps,90):>9.2f}"
              f"{pct(deps,99):>9.2f}{pct(deps,100):>9.2f}{srat:>9.2f}"
              f"{100*len(exited)/len(recs):>6.0f}%{(100*cut/len(exited) if exited else 0):>8.0f}%")
    print("\ndep_* = USDC deployed per window-side (buy notional). sell/buy = median exit ratio")
    print("(0=hold to settlement). exit% = windows where they sold >5% back. cutloss% = of those")
    print("exits, how many sold BELOW their buy price (stop-loss vs spread capture).")


if __name__ == "__main__":
    main()
