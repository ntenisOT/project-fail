"""Read-only ClickHouse queries for set-trader wallet analysis."""

from __future__ import annotations

from typing import Sequence

from clickhouse_connect.driver.external import ExternalData  # type: ignore[import-untyped]

from tools.market_windows import ResolvedWindow
from tools.wallet_metrics import TokenActivity


SETTINGS = {"max_query_size": 300_000_000, "enable_analyzer": 1}
LIFECYCLE_LOOKBACK_S = 26 * 60 * 60
LIFECYCLE_TAIL_S = 60 * 60


def window_external_data(windows: Sequence[ResolvedWindow]) -> ExternalData:
    records: list[str] = []
    for window in windows:
        window.validate()
        for token, side, payoff in (
            (window.up_token, 1, window.winner_up),
            (window.down_token, 0, 1 - window.winner_up),
        ):
            records.append("\t".join((
                window.slug, window.asset, str(window.start), window.condition_id,
                token, str(side), str(float(payoff)),
            )))
    return ExternalData(
        data=("\n".join(records) + "\n").encode(),
        file_name="set_windows",
        fmt="TabSeparated",
        structure=("slug String, asset String, start_ts UInt32, condition_id String, "
                   "token String, side UInt8, payoff Float64"),
    )


def _legs_sql(t0: int, t1: int) -> str:
    """Normalize user order fills across legacy and V2 exchange events.

    V2 emits one ``OrderFilled`` per participating order.  The taker order's
    summary row uses the exchange contract as ``taker``; treating every row as
    a bilateral trade duplicates that order and can turn complementary buys
    into a fabricated buy/sell cycle.  V2 rows therefore contribute only the
    order owner's (``maker`` field) leg.  Legacy transactions retain the old
    bilateral mapping.
    """
    period = f"""
      FROM trade_history
      WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
        AND (maker_asset_id IN (SELECT token FROM set_windows)
             OR taker_asset_id IN (SELECT token FROM set_windows))
    """
    exchanges = (
        "'0xe111180000d2663c0091e4f400237545b87b996b',"
        "'0xe2222d279d744050d28e00520010520000310f59'"
    )
    return f"""
      WITH v2_transactions AS (
        SELECT tx_hash
        FROM trade_history
        WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
          AND lower(taker) IN ({exchanges})
          AND (maker_asset_id IN (SELECT token FROM set_windows)
               OR taker_asset_id IN (SELECT token FROM set_windows))
        GROUP BY tx_hash
      )
      SELECT maker AS wallet,
             multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
             toUInt32(block_timestamp) AS ts,
             block_number, log_index,
             toFloat64(if(maker_asset_id='0', maker_amount_filled,
                          taker_amount_filled))/1e6 AS usdc,
             if(maker_asset_id='0', -usdc, usdc) AS cash,
             toFloat64(if(maker_asset_id='0', taker_amount_filled,
                          maker_amount_filled))/1e6 AS shares,
             if(maker_asset_id='0', shares, -shares) AS net_shares,
             if(maker_asset_id='0', shares, 0.0) AS bought,
             if(maker_asset_id='0', 0.0, shares) AS sold,
             lower(taker) NOT IN ({exchanges}) AS is_maker,
             toFloat64(fee)/1e6 AS taker_fee
      {period}
        AND tx_hash IN v2_transactions
      UNION ALL
      SELECT if(maker_asset_id='0', maker, taker) AS wallet,
             multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
             toUInt32(block_timestamp) AS ts,
             block_number, log_index,
             toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
             -usdc AS cash,
             toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS shares,
             shares AS net_shares, shares AS bought, 0.0 AS sold,
             maker_asset_id='0' AS is_maker,
             if(is_maker, 0.0, toFloat64(fee)/1e6) AS taker_fee
      {period}
        AND tx_hash NOT IN v2_transactions
      UNION ALL
      SELECT if(maker_asset_id='0', taker, maker) AS wallet,
             multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
             toUInt32(block_timestamp) AS ts,
             block_number, log_index,
             toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
             usdc AS cash,
             toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS shares,
             -shares AS net_shares, 0.0 AS bought, shares AS sold,
             maker_asset_id!='0' AS is_maker,
             if(is_maker, 0.0, toFloat64(fee)/1e6) AS taker_fee
      {period}
        AND tx_hash NOT IN v2_transactions
    """


def fetch_token_activity(client, windows: Sequence[ResolvedWindow]) -> list[TokenActivity]:
    """Fetch each token's full trading lifecycle, not only its 5-minute event.

    Gamma currently opens these markets about 24 hours before the timestamp in
    the slug. Restricting fills to ``start_ts..start_ts+300`` fabricates both
    inventory deficits and PnL by dropping pre-window acquisition.
    """
    if not windows:
        return []
    t0 = min(w.start for w in windows) - LIFECYCLE_LOOKBACK_S
    t1 = max(w.start for w in windows) + LIFECYCLE_TAIL_S
    query = f"""
    SELECT lower(l.wallet), w.slug, any(w.asset), any(w.start_ts), w.side,
           sum(l.cash-l.taker_fee) + sum(l.net_shares*w.payoff), sum(l.usdc),
           sum(l.bought), sumIf(l.usdc, l.bought>0),
           sum(l.sold), sumIf(l.usdc, l.sold>0), sum(l.net_shares),
           sumIf(l.usdc, l.is_maker), count(), countIf(l.is_maker),
           sumIf(l.taker_fee, l.bought>0), sumIf(l.taker_fee, l.sold>0)
    FROM ({_legs_sql(t0, t1)}) l
    INNER JOIN set_windows w ON l.token=w.token
    WHERE l.wallet!=''
    GROUP BY lower(l.wallet), w.slug, w.side
    """
    rows = client.query(
        query, settings=SETTINGS, external_data=window_external_data(windows)
    ).result_rows
    return [TokenActivity(*row) for row in rows]


def fetch_direct_ctf(
    client, windows: Sequence[ResolvedWindow]
) -> dict[tuple[str, str], tuple[float, float]]:
    """Return same-address CTF split/merge sets; zeros do not exclude proxies."""
    if not windows:
        return {}
    t0 = min(w.start for w in windows) - LIFECYCLE_LOOKBACK_S
    t1 = max(w.start for w in windows) + LIFECYCLE_TAIL_S
    query = f"""
    SELECT lower(sm.stakeholder), w.slug,
           sumIf(toFloat64(sm.amount)/1e6, sm.op='split'),
           sumIf(toFloat64(sm.amount)/1e6, sm.op='merge')
    FROM splits_merges sm
    INNER JOIN (SELECT DISTINCT slug, condition_id FROM set_windows) w
      ON sm.condition_id=w.condition_id
    WHERE sm.block_timestamp>=toDateTime({t0}) AND sm.block_timestamp<toDateTime({t1})
    GROUP BY lower(sm.stakeholder), w.slug
    """
    rows = client.query(
        query, settings=SETTINGS, external_data=window_external_data(windows)
    ).result_rows
    return {(str(wallet), str(slug)): (float(split), float(merge))
            for wallet, slug, split, merge in rows}
