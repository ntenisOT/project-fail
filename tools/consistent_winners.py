"""Rank wallets by CONSISTENCY of daily profit, not by margin.

Why this exists. We originally ranked by margin = pnl/volume and concluded
that selective, thin traders win. That is circular: shrinking the denominator
raises margin by construction, so the metric selects for thinness and then we
"discovered" thinness. The wallet we chose to copy on that basis (0xce50c96b)
trades in bursts and sat idle for 18 hours; meanwhile the largest dollar
winner in the same period ran a 0.42% margin - near the bottom of our own
ranking - on $139M of volume, always-on and 99.5% maker.

Consistency is the better objective: a wallet that is profitable on most days
at real volume is running a repeatable process, which is what can be copied.
A single lucky whale bet is not.

Methodology, corrected against the exact bug Codex found in
tools/winner_persistence.py:
  * Period A is ranked using ONLY period-A data. No filter references period B.
  * The full period-A top-N is carried forward, INCLUDING wallets that stop
    trading. A wallet that vanishes counts as a failure of the strategy, not
    as a row to drop. Dropping them is selection on the future.
  * Both periods use the same market universe and do not share a boundary day.

PnL is settlement-aware: cash flow plus the redemption value of shares still
held in the winning token. Cash flow alone is meaningless for wallets that
buy and hold to $1, which is most of this cohort.

Read-only.
"""
from __future__ import annotations

import argparse
import statistics

import clickhouse_connect  # type: ignore[import-untyped]
from clickhouse_connect.driver.external import ExternalData  # type: ignore[import-untyped]

# trade_history.condition_id is empty on ~98% of rows, so the market a trade
# belongs to has to be recovered from the asset ids (which are 100% populated)
# and mapped through token_id_map. Grouping on condition_id directly silently
# returns almost nothing.
SHORT_MARKET_SQL = """
SELECT token FROM (
  SELECT multiIf(maker_asset_id != '0', maker_asset_id, taker_asset_id) AS token,
         count() AS n,
         max(toUnixTimestamp(block_timestamp))
           - min(toUnixTimestamp(block_timestamp)) AS life
  FROM trade_history
  WHERE block_timestamp >= toDateTime({t0:UInt32})
    AND block_timestamp <  toDateTime({t1:UInt32})
  GROUP BY token
  HAVING life < 900 AND n > 50
)
"""

DAILY_PNL_SQL = """
WITH winners AS (
  -- The winner is derived from the tape, NOT from cid_winner/token_id_map.
  -- Neither is usable here: token_id_map holds 0 of these 5m tokens, and
  -- cid_winner stopped being populated on 2026-08-22, so any settlement
  -- taken from it would be silently stale.
  -- A resolved binary's last trade sits at ~1.00 for the winner and ~0.00
  -- for the loser, so the final traded price separates them cleanly.
  SELECT token AS token_id, argMax(px, ts) >= 0.9 AS is_winner
  FROM (
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
          IN (SELECT token_id FROM tokens)
  )
  WHERE px IS NOT NULL
  GROUP BY token
),
merged AS (
  SELECT wallet, day, token_id,
         toFloat64(sumMerge(buy_sh))    / 1e6 AS bsh,
         toFloat64(sumMerge(buy_usdc))  / 1e6 AS busd,
         toFloat64(sumMerge(sell_sh))   / 1e6 AS ssh,
         toFloat64(sumMerge(sell_usdc)) / 1e6 AS susd,
         toUInt64(countMerge(fills))          AS f
  FROM wallet_token_day_agg
  WHERE day >= {d0:Date} AND day <= {d1:Date}
    AND token_id IN (SELECT token_id FROM tokens)
  GROUP BY wallet, day, token_id
)
SELECT m.wallet AS wallet,
       m.day    AS day,
       sum(m.susd - m.busd + if(w.is_winner, m.bsh - m.ssh, 0)) AS pnl,
       sum(m.busd)                                              AS volume,
       sum(m.f)                                                 AS fills
FROM merged AS m
INNER JOIN winners AS w ON w.token_id = m.token_id
GROUP BY wallet, day
"""


def fetch(c, d0: str, d1: str, t0: int, t1: int):
    markets = [r[0] for r in c.query(
        SHORT_MARKET_SQL, parameters={"t0": t0, "t1": t1}).result_rows]
    print(f"  short-lived tokens in window: {len(markets)}")
    if not markets:
        return {}, 0
    # 10k+ token ids will not fit in an HTTP parameter; ship them as a
    # temporary table the way tools/clickhouse_forensics.py does.
    external = ExternalData(
        data="\n".join(markets).encode() + b"\n",
        file_name="tokens", fmt="TabSeparated", structure="token_id String")
    rows = c.query(DAILY_PNL_SQL,
                   parameters={"d0": d0, "d1": d1, "t0": t0, "t1": t1},
                   external_data=external).result_rows
    per: dict[str, list] = {}
    for wallet, day, pnl, volume, fills in rows:
        per.setdefault(wallet, []).append((day, float(pnl), float(volume), int(fills)))
    return per, len(markets)


def profile(days: list) -> dict:
    pnls = [p for _, p, _, _ in days]
    return {
        "days": len(days),
        "win_days": sum(1 for p in pnls if p > 0),
        "consistency": sum(1 for p in pnls if p > 0) / len(pnls),
        "total_pnl": sum(pnls),
        "median_day": statistics.median(pnls),
        "volume": sum(v for _, _, v, _ in days),
        "fills": sum(f for _, _, _, f in days),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-start", required=True)
    ap.add_argument("--a-end", required=True)
    ap.add_argument("--b-start", required=True)
    ap.add_argument("--b-end", required=True)
    ap.add_argument("--min-days", type=int, default=3)
    ap.add_argument("--min-volume", type=float, default=5000.0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    import datetime
    def epoch(d: str) -> int:
        return int(datetime.datetime.strptime(d, "%Y-%m-%d")
                   .replace(tzinfo=datetime.timezone.utc).timestamp())

    c = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
        settings={"max_execution_time": 1800, "max_memory_usage": 12_000_000_000})

    a_raw, a_markets = fetch(c, args.a_start, args.a_end,
                             epoch(args.a_start), epoch(args.a_end) + 86400)
    b_raw, b_markets = fetch(c, args.b_start, args.b_end,
                             epoch(args.b_start), epoch(args.b_end) + 86400)
    print(f"period A {args.a_start}..{args.a_end}: {a_markets} short markets, "
          f"{len(a_raw)} wallets")
    print(f"period B {args.b_start}..{args.b_end}: {b_markets} short markets, "
          f"{len(b_raw)} wallets\n")

    # rank period A on period-A data ONLY
    a = {w: profile(d) for w, d in a_raw.items()}
    eligible = {w: p for w, p in a.items()
                if p["days"] >= args.min_days and p["volume"] >= args.min_volume}
    ranked = sorted(eligible.items(),
                    key=lambda kv: (kv[1]["consistency"], kv[1]["total_pnl"]),
                    reverse=True)[:args.top]

    b = {w: profile(d) for w, d in b_raw.items()}
    print(f"TOP {len(ranked)} BY PERIOD-A CONSISTENCY, carried forward in full")
    print(f"{'wallet':<44}{'A cons':>8}{'A pnl$':>11}{'A vol$':>12}"
          f"{'| B cons':>10}{'B pnl$':>11}")
    survived = kept = 0
    b_pnls = []
    for wallet, pa in ranked:
        pb = b.get(wallet)
        if pb is None:
            print(f"{wallet:<44}{pa['consistency']:>7.0%}{pa['total_pnl']:>11,.0f}"
                  f"{pa['volume']:>12,.0f}{'|  GONE':>10}{'-':>11}")
            continue
        survived += 1
        kept += pb["total_pnl"] > 0
        b_pnls.append(pb["total_pnl"])
        print(f"{wallet:<44}{pa['consistency']:>7.0%}{pa['total_pnl']:>11,.0f}"
              f"{pa['volume']:>12,.0f}{pb['consistency']:>9.0%}"
              f"{pb['total_pnl']:>11,.0f}")

    print(f"\nsurvived into period B: {survived}/{len(ranked)}  "
          f"(wallets that vanished are counted as failures, not dropped)")
    if b_pnls:
        print(f"profitable in B: {kept}/{len(ranked)} of the ORIGINAL top list")
        print(f"median period-B pnl among survivors: ${statistics.median(b_pnls):,.0f}")
    pop = [p["total_pnl"] for p in b.values() if p["volume"] >= args.min_volume]
    if pop:
        print(f"population median period-B pnl: ${statistics.median(pop):,.0f} "
              f"(n={len(pop)})")


if __name__ == "__main__":
    main()
