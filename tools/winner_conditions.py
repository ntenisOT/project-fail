"""What is different about the days 0xce50c96b trades?

He is not dormant - he is bursty: ~9.4k trades on Aug 22, zero on Aug 23 and
24, ~9.9k on Aug 25, zero on Aug 26. If we can name the condition that
switches him on, that is worth more than copying his order flow, because it
tells us WHEN our own arms should be quoting at all.

Hypothesis under test: he trades when the market is moving decisively in one
direction, and stands aside when it is choppy.

Per day, over the 5-minute windows, this measures:
  early_vol   stddev of traded price during the first 180s of each window,
              averaged over windows. High = price thrashing before the
              outcome is clear. Low = steady drift.
  decisive    mean |price - 0.5| at T+120..180. High = the market already
              knows the answer by mid-window (a directional move). Low = a
              coin flip into the last minute.
  late_share  fraction of each window's trades in its final 60s. High = the
              action is all at the death, i.e. undecided.

Settlement is not needed for any of these, which matters because cid_winner
has been dead since 2026-08-22 and token_id_map holds none of these tokens.

Read-only.
"""
from __future__ import annotations

import argparse

import clickhouse_connect  # type: ignore[import-untyped]

WALLET = "0xce50C96B976203b53342a0a801067d2cdCfCf46E"

SQL = """
WITH legs AS (
  SELECT multiIf(maker_asset_id != '0', maker_asset_id, taker_asset_id) AS token,
         toUnixTimestamp(block_timestamp) AS ts,
         toFloat64(if(maker_asset_id = '0', maker_amount_filled,
                      taker_amount_filled))
           / nullIf(toFloat64(if(maker_asset_id = '0', taker_amount_filled,
                                 maker_amount_filled)), 0) AS px
  FROM trade_history
  WHERE block_timestamp >= toDateTime({t0:UInt32})
    AND block_timestamp <  toDateTime({t1:UInt32})
),
bounds AS (
  SELECT token, min(ts) AS t_open, max(ts) AS t_close, count() AS n
  FROM legs GROUP BY token
  HAVING (t_close - t_open) < 900 AND n > 50
),
joined AS (
  SELECT l.token AS token, l.ts - b.t_open AS elapsed, l.px AS px,
         b.t_open AS t_open, b.t_close - b.t_open AS life
  FROM legs AS l INNER JOIN bounds AS b ON b.token = l.token
  WHERE l.px IS NOT NULL AND l.px > 0 AND l.px < 1
)
SELECT toDate(toDateTime(t_open))                       AS day,
       uniqExact(token)                                 AS windows,
       round(avg(early_vol), 4)                         AS early_vol,
       round(avg(decisive), 4)                          AS decisive,
       round(avg(late_share), 4)                        AS late_share
FROM (
  SELECT token, any(t_open) AS t_open,
         stddevPopIf(px, elapsed <= 180)                AS early_vol,
         avgIf(abs(px - 0.5), elapsed BETWEEN 120 AND 180) AS decisive,
         countIf(elapsed >= life - 60) / count()        AS late_share
  FROM joined GROUP BY token
  HAVING isFinite(early_vol) AND isFinite(decisive)
)
GROUP BY day ORDER BY day
"""

ACTIVITY = """
SELECT toDate(block_timestamp) AS day, count() AS trades
FROM trade_history
WHERE block_timestamp >= toDateTime({t0:UInt32})
  AND block_timestamp <  toDateTime({t1:UInt32})
  AND (lower(maker) = lower({w:String}) OR lower(taker) = lower({w:String}))
GROUP BY day ORDER BY day
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=9)
    ap.add_argument("--wallet", default=WALLET)
    args = ap.parse_args()

    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    t1 = int(now.timestamp())
    t0 = t1 - args.days * 86400

    c = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
        settings={"max_execution_time": 1800, "max_memory_usage": 12_000_000_000})

    market = {r[0]: r[1:] for r in
              c.query(SQL, parameters={"t0": t0, "t1": t1}).result_rows}
    acts = {r[0]: r[1] for r in c.query(
        ACTIVITY, parameters={"t0": t0, "t1": t1, "w": args.wallet}).result_rows}

    print(f"wallet {args.wallet}\n")
    print(f"{'day':<12}{'windows':>9}{'early_vol':>11}{'decisive':>10}"
          f"{'late_share':>12}{'his trades':>12}")
    on, off = [], []
    for day in sorted(market):
        windows, early, decisive, late = market[day]
        n = int(acts.get(day, 0))
        (on if n > 0 else off).append((early, decisive, late))
        flag = "" if n else "   <- IDLE"
        print(f"{str(day):<12}{windows:>9}{early:>11.4f}{decisive:>10.4f}"
              f"{late:>12.4f}{n:>12,}{flag}")

    if on and off:
        def mean(rows, i):
            return sum(r[i] for r in rows) / len(rows)
        print(f"\n{'':12}{'early_vol':>20}{'decisive':>10}{'late_share':>12}")
        print(f"{'TRADING':<12}{mean(on,0):>20.4f}{mean(on,1):>10.4f}"
              f"{mean(on,2):>12.4f}   (n={len(on)} days)")
        print(f"{'IDLE':<12}{mean(off,0):>20.4f}{mean(off,1):>10.4f}"
              f"{mean(off,2):>12.4f}   (n={len(off)} days)")
        print("\nIf 'decisive' is clearly higher on trading days, he waits for")
        print("directional markets. If the columns barely move, the trigger is")
        print("something these metrics do not see and the hypothesis is wrong.")


if __name__ == "__main__":
    main()
