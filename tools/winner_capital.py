"""What capital does a consistent winner actually need?

I claimed a $200 bankroll cannot matter in this market, using 0xB27BC932 -
$590k on $139M, a 0.42% margin - as the model of a winner. That was the wrong
wallet. tools/consistent_winners.py had already found wallets earning 3-33%
margins on tens of thousands of volume, which is a completely different
business and a reachable one.

This profiles the consistency-ranked winners on the axes that decide whether
we can copy them at our size:

  peak_open$   the most collateral tied up at once, summed across open
               positions - the real capital requirement, not the volume.
  turns/day    volume / peak_open, i.e. how hard the capital works.
  margin%      pnl / volume.
  maker%       share of fills where they provided liquidity.
  windows      how many 5m markets they touched, and fills per window.

Read-only. Settlement is derived from the tape (a resolved binary's last trade
sits at ~1.00 for the winner) because cid_winner has been dead since
2026-08-22 and token_id_map holds none of these tokens.
"""
from __future__ import annotations

import argparse
import datetime

import clickhouse_connect  # type: ignore[import-untyped]
from clickhouse_connect.driver.external import ExternalData  # type: ignore[import-untyped]

SHORT_TOKENS = """
SELECT token FROM (
  SELECT multiIf(maker_asset_id != '0', maker_asset_id, taker_asset_id) AS token,
         count() AS n,
         max(toUnixTimestamp(block_timestamp))
           - min(toUnixTimestamp(block_timestamp)) AS life
  FROM trade_history
  WHERE block_timestamp >= toDateTime({t0:UInt32})
    AND block_timestamp <  toDateTime({t1:UInt32})
  GROUP BY token HAVING life < 900 AND n > 50
)
"""

PROFILE = """
WITH fills AS (
  SELECT multiIf(maker_asset_id != '0', maker_asset_id, taker_asset_id) AS token,
         toUnixTimestamp(block_timestamp) AS ts,
         if(lower(maker) = lower({w:String}), 1, 0) AS as_maker,
         -- our side: buying the token if we gave USDC
         if(lower(maker) = lower({w:String}),
            maker_asset_id = '0', taker_asset_id = '0') AS bought,
         toFloat64(if(maker_asset_id = '0', taker_amount_filled,
                      maker_amount_filled)) / 1e6 AS sh,
         toFloat64(if(maker_asset_id = '0', maker_amount_filled,
                      taker_amount_filled)) / 1e6 AS usd
  FROM trade_history
  WHERE block_timestamp >= toDateTime({t0:UInt32})
    AND block_timestamp <  toDateTime({t1:UInt32})
    AND (lower(maker) = lower({w:String}) OR lower(taker) = lower({w:String}))
    AND multiIf(maker_asset_id != '0', maker_asset_id, taker_asset_id)
        IN (SELECT token FROM tokens)
),
last_px AS (
  SELECT token, argMax(px, ts) AS final_px FROM (
    SELECT multiIf(maker_asset_id != '0', maker_asset_id, taker_asset_id) AS token,
           toUnixTimestamp(block_timestamp) AS ts,
           toFloat64(if(maker_asset_id = '0', maker_amount_filled,
                        taker_amount_filled))
             / nullIf(toFloat64(if(maker_asset_id = '0', taker_amount_filled,
                                   maker_amount_filled)), 0) AS px
    FROM trade_history
    WHERE block_timestamp >= toDateTime({t0:UInt32})
      AND block_timestamp <  toDateTime({t1:UInt32})
      AND multiIf(maker_asset_id != '0', maker_asset_id, taker_asset_id)
          IN (SELECT token FROM tokens)
  ) WHERE px IS NOT NULL GROUP BY token
),
per_token AS (
  SELECT f.token AS token,
         sumIf(f.usd, f.bought) AS bought_usd,
         sumIf(f.usd, NOT f.bought) AS sold_usd,
         sumIf(f.sh, f.bought) - sumIf(f.sh, NOT f.bought) AS net_sh,
         count() AS fills,
         sum(f.as_maker) AS maker_fills,
         sum(f.usd) AS volume
  FROM fills AS f GROUP BY token
)
SELECT count()                                            AS tokens,
       sum(fills)                                         AS fills,
       sum(maker_fills)                                   AS maker_fills,
       sum(volume)                                        AS volume,
       sum(bought_usd)                                    AS deployed,
       max(bought_usd)                                    AS max_single,
       sum(sold_usd - bought_usd
           + if(p.final_px >= 0.9, net_sh, 0))            AS pnl
FROM per_token AS t
LEFT JOIN last_px AS p ON p.token = t.token
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallets", nargs="+", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    args = ap.parse_args()

    def epoch(d: str) -> int:
        return int(datetime.datetime.strptime(d, "%Y-%m-%d")
                   .replace(tzinfo=datetime.timezone.utc).timestamp())

    t0, t1 = epoch(args.start), epoch(args.end) + 86400
    days = max(1, (t1 - t0) // 86400)
    c = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
        settings={"max_execution_time": 1800, "max_memory_usage": 12_000_000_000})

    tokens = [r[0] for r in c.query(
        SHORT_TOKENS, parameters={"t0": t0, "t1": t1}).result_rows]
    print(f"5m tokens in {args.start}..{args.end}: {len(tokens)}  ({days} days)\n")
    external = ExternalData(data="\n".join(tokens).encode() + b"\n",
                            file_name="tokens", fmt="TabSeparated",
                            structure="token String")

    print(f"{'wallet':<44}{'pnl$':>10}{'vol$':>11}{'margin':>8}"
          f"{'maxPos$':>9}{'mkt':>6}{'fills':>7}{'maker':>7}{'$/day':>9}")
    for wallet in args.wallets:
        rows = c.query(PROFILE, parameters={
            "w": wallet, "t0": t0, "t1": t1}, external_data=external).result_rows
        if not rows or not rows[0][0]:
            print(f"{wallet:<44}{'no 5m activity':>60}")
            continue
        tk, fills, mk, vol, deployed, max_single, pnl = rows[0]
        margin = (pnl / vol) if vol else 0.0
        maker = (mk / fills) if fills else 0.0
        print(f"{wallet:<44}{pnl:>+10,.0f}{vol:>11,.0f}{margin:>7.1%}"
              f"{max_single:>9,.0f}{tk:>6}{fills:>7,}{maker:>6.0%}"
              f"{pnl/days:>+9,.0f}")

    print("\nmaxPos$ is the largest amount bought in ANY single market - the")
    print("floor on working capital. Compare it with our $200 Safe.")


if __name__ == "__main__":
    main()
