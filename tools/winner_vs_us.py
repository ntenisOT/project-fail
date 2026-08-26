"""Compare the wallet we tried to copy against the current market regime.

Two questions this answers, both raised by the independent review round:

  1. Is 0xce50c96b - the top wallet whose behaviour the basket board was built
     to approximate - still making money right now, or did it stop when the
     August liquidity-reward programme ended?
  2. Is there still organic flow in the 5-minute crypto windows at all?

If the winner has gone quiet along with the flow, then every strategy result
measured Aug 18-25 describes a regime that no longer exists, and copying it is
copying a subsidy rather than an edge.

Read-only. Cash flow here is realized trade cash (sells - buys), NOT settlement
PnL: open inventory and redemptions are not included, so a wallet that is long
into resolution looks worse than it is. Direction of change across days is the
signal, not the absolute level.
"""
from __future__ import annotations

import argparse

import clickhouse_connect  # type: ignore[import-untyped]

WINNER_PREFIX = "0xce50c96b"


def client():
    return clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
        settings={"max_execution_time": 900, "max_memory_usage": 8_000_000_000},
    )


def resolve_wallet(c, prefix: str) -> str | None:
    rows = c.query(
        "SELECT wallet, sum(toUInt64(fills_count)) AS f FROM ("
        "  SELECT wallet, countMerge(fills_count) AS fills_count"
        "  FROM trader_scores_daily_agg WHERE day >= today() - 30 GROUP BY wallet"
        ") WHERE lower(wallet) LIKE {p:String} GROUP BY wallet ORDER BY f DESC LIMIT 5",
        parameters={"p": prefix.lower() + "%"},
    ).result_rows
    return rows[0][0] if rows else None


def winner_daily(c, wallet: str, days: int) -> list[tuple]:
    return c.query(
        "SELECT day,"
        "       countMerge(fills_count) AS fills,"
        "       round(toFloat64(sumMerge(buys_usdc_sum)) / 1e6, 2) AS bought,"
        "       round(toFloat64(sumMerge(sells_usdc_sum)) / 1e6, 2) AS sold,"
        "       round((toFloat64(sumMerge(sells_usdc_sum))"
        "              - toFloat64(sumMerge(buys_usdc_sum))) / 1e6, 2) AS net_cash,"
        "       uniqExactMerge(unique_conditions) AS markets "
        "FROM trader_scores_daily_agg "
        "WHERE wallet = {w:String} AND day >= today() - {d:UInt32} "
        "GROUP BY day ORDER BY day",
        parameters={"w": wallet, "d": days},
    ).result_rows


def regime_daily(c, days: int) -> list[tuple]:
    """Daily flow in 5-minute crypto windows, from market slugs."""
    return c.query(
        "SELECT toDate(t.block_timestamp) AS day,"
        "       count() AS trades,"
        "       uniqExact(m.condition_id) AS markets,"
        "       round(count() / nullIf(uniqExact(m.condition_id), 0), 1) AS per_market "
        "FROM trade_history AS t "
        "INNER JOIN markets_meta AS m ON m.condition_id = t.condition_id "
        "WHERE t.block_timestamp >= now() - INTERVAL {d:UInt32} DAY "
        "  AND m.slug LIKE '%-updown-5m-%' "
        "GROUP BY day ORDER BY day",
        parameters={"d": days},
    ).result_rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=12)
    ap.add_argument("--wallet", default=WINNER_PREFIX)
    args = ap.parse_args()
    c = client()

    wallet = args.wallet if args.wallet.startswith("0x") and len(args.wallet) == 42 \
        else resolve_wallet(c, args.wallet)
    if not wallet:
        raise SystemExit(f"no wallet matching {args.wallet}")

    print(f"WINNER {wallet}")
    print(f"{'day':<12}{'fills':>8}{'bought$':>12}{'sold$':>12}"
          f"{'net_cash$':>12}{'markets':>9}")
    for day, fills, bought, sold, net, markets in winner_daily(c, wallet, args.days):
        print(f"{str(day):<12}{fills:>8}{bought:>12.2f}{sold:>12.2f}"
              f"{net:>+12.2f}{markets:>9}")

    print(f"\n5-MINUTE CRYPTO WINDOW FLOW (all traders)")
    print(f"{'day':<12}{'trades':>10}{'markets':>10}{'trades/mkt':>12}")
    for day, trades, markets, per in regime_daily(c, args.days):
        print(f"{str(day):<12}{trades:>10}{markets:>10}{(per or 0):>12.1f}")

    print("\nnet_cash is realized trade cash (sells-buys), not settlement PnL;")
    print("open inventory and redemptions are excluded. Read the trend, not the level.")


if __name__ == "__main__":
    main()
