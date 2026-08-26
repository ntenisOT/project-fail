"""Exact bounded ClickHouse extraction for the mint accounting artifact."""

from __future__ import annotations

from typing import Any, Sequence

from tools.clickhouse_forensics import SETTINGS, window_external_data
from tools.market_windows import ResolvedWindow
from tools.mint_accounting_inputs import EvidenceError, integer


V2_EXCHANGES = (
    "0xe111180000d2663c0091e4f400237545b87b996b",
    "0xe2222d279d744050d28e00520010520000310f59",
)
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
EXPECTED_COLUMNS = {
    "trade_history": {
        "block_number": "UInt64", "block_timestamp": "DateTime64(0, 'UTC')",
        "tx_hash": "FixedString(66)", "log_index": "UInt16", "order_hash": "String",
        "maker": "LowCardinality(String)", "taker": "LowCardinality(String)",
        "maker_asset_id": "String", "taker_asset_id": "String",
        "maker_amount_filled": "UInt256", "taker_amount_filled": "UInt256",
        "fee": "UInt256", "is_neg_risk": "Bool", "condition_id": "String",
        "outcome_index": "UInt8",
    },
    "redemptions": {
        "block_number": "UInt64", "block_timestamp": "DateTime64(0, 'UTC')",
        "tx_hash": "FixedString(66)", "log_index": "UInt16",
        "redeemer": "LowCardinality(String)", "condition_id": "String",
        "collateral_token": "LowCardinality(String)", "parent_collection_id": "String",
        "index_sets": "Array(UInt256)", "payout": "UInt256",
    },
    "erc1155_transfers": {
        "block_timestamp": "DateTime64(0, 'UTC')", "from_addr": "LowCardinality(String)",
        "to_addr": "LowCardinality(String)", "token_id": "String",
    },
    "usdc_transfers": {
        "block_timestamp": "DateTime64(0, 'UTC')", "token_addr": "LowCardinality(String)",
        "from_addr": "LowCardinality(String)", "to_addr": "LowCardinality(String)",
        "value": "UInt256",
    },
}


def _fill_sql(t0: int, t1: int, wallet: str) -> str:
    exchanges = ",".join(f"'{address}'" for address in V2_EXCHANGES)
    return f"""
WITH v2_transactions AS (
  SELECT tx_hash FROM trade_history FINAL
  WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
    AND lower(taker) IN ({exchanges})
    AND (maker_asset_id IN (SELECT token FROM set_windows)
         OR taker_asset_id IN (SELECT token FROM set_windows))
  GROUP BY tx_hash
)
SELECT block_number, toUInt32(block_timestamp), log_index, lower(toString(tx_hash)),
       order_hash, lower(maker), lower(taker), maker_asset_id, taker_asset_id,
       toString(maker_amount_filled), toString(taker_amount_filled), toString(fee),
       is_neg_risk, lower(condition_id), outcome_index,
       tx_hash IN v2_transactions,
       lower(maker)='{wallet}' AND lower(taker) NOT IN ({exchanges})
FROM trade_history FINAL
WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
  AND (lower(maker)='{wallet}' OR lower(taker)='{wallet}')
  AND (maker_asset_id IN (SELECT token FROM set_windows)
       OR taker_asset_id IN (SELECT token FROM set_windows))
ORDER BY block_number, log_index
""".strip()


def _redemption_sql(t0: int, t1: int, wallet: str) -> str:
    return f"""
SELECT block_number, toUInt32(block_timestamp), log_index, lower(toString(tx_hash)),
       lower(redeemer), lower(condition_id), lower(collateral_token),
       parent_collection_id, index_sets, toString(payout)
FROM redemptions FINAL
WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
  AND lower(redeemer)='{wallet}'
  AND lower(condition_id) IN (SELECT lower(condition_id) FROM set_windows GROUP BY condition_id)
ORDER BY block_number, log_index
""".strip()


def source_rows(client: Any, windows: Sequence[ResolvedWindow], wallet: str,
                t0: int, t1: int) -> tuple[list[tuple], list[tuple], dict[str, object]]:
    for table, expected in EXPECTED_COLUMNS.items():
        actual = {str(row[0]): str(row[1]) for row in client.query(
            f"DESCRIBE TABLE {table}").result_rows}
        if any(actual.get(name) != kind for name, kind in expected.items()):
            raise EvidenceError(f"ClickHouse schema mismatch for {table}")
    watermarks = {
        table: int(client.command(f"SELECT toUInt32(max(block_timestamp)) FROM {table}"))
        for table in ("trade_history", "redemptions", "erc1155_transfers")
    }
    if any(value < t1 for value in watermarks.values()):
        raise EvidenceError("a required ClickHouse source ends before the lifecycle tail")
    fills_sql, redemptions_sql = _fill_sql(t0, t1, wallet), _redemption_sql(t0, t1, wallet)
    fills = client.query(fills_sql, settings=SETTINGS,
                         external_data=window_external_data(windows)).result_rows
    redemptions = client.query(
        redemptions_sql, settings=SETTINGS, external_data=window_external_data(windows),
    ).result_rows
    erc_sql = f"""SELECT count(), countIf(token_id IN (SELECT token FROM set_windows)),
      countIf(lower(from_addr)='{wallet}' OR lower(to_addr)='{wallet}'),
      countIf(token_id IN (SELECT token FROM set_windows)
        AND (lower(from_addr)='{wallet}' OR lower(to_addr)='{wallet}'))
      FROM erc1155_transfers FINAL
      WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})"""
    erc = client.query(erc_sql, settings=SETTINGS,
                       external_data=window_external_data(windows)).result_rows[0]
    if int(erc[1]) != 0:
        raise EvidenceError(
            "mapped ERC-1155 transfers exist but are not integrated into the v2 ledger"
        )
    coverage = {
        "clickhouse_version": str(client.command("SELECT version()")),
        "source_watermark_unix_s": watermarks,
        "erc1155_interval_rows": int(erc[0]),
        "erc1155_mapped_token_rows": int(erc[1]),
        "erc1155_target_address_rows": int(erc[2]),
        "erc1155_mapped_target_address_rows": int(erc[3]),
        "erc1155_coverage_status": "known_incomplete_token_mapping_not_custody_complete",
        "erc1155_custody_complete": False,
        "usdc_transfers_global_rows": int(client.command(
            "SELECT count() FROM usdc_transfers FINAL")),
        "sql": {"fills": fills_sql, "redemptions": redemptions_sql,
                "erc1155_coverage": erc_sql},
    }
    return fills, redemptions, coverage


def fill_events(rows: Sequence[tuple], windows: Sequence[ResolvedWindow],
                wallet: str) -> list[dict[str, object]]:
    token_map = {
        token: (window, side_up)
        for window in windows
        for token, side_up in ((window.up_token, True), (window.down_token, False))
    }
    events: list[dict[str, object]] = []
    seen: set[tuple[int, int]] = set()
    for row in rows:
        (block, timestamp, log_index, tx_hash, order_hash, maker, taker,
         maker_asset, taker_asset, maker_amount, taker_amount, fee, neg_risk,
         raw_condition, outcome_index, is_v2, is_maker) = row
        key = (int(block), int(log_index))
        if key in seen:
            raise EvidenceError("duplicate owner fill block/log key")
        seen.add(key)
        if str(maker) == str(taker):
            raise EvidenceError("self-trades are forbidden in mint accounting")
        if (str(maker) != wallet or not is_v2 or not is_maker or neg_risk
                or str(maker_asset) == "0" or str(taker_asset) != "0"
                or integer(fee, "fee") != 0):
            raise EvidenceError("target fill is not a fee-zero V2 maker sale")
        mapped = token_map.get(str(maker_asset))
        if mapped is None:
            raise EvidenceError("target fill token is outside the frozen mapping")
        window, side_up = mapped
        raw_condition = str(raw_condition).lower()
        if raw_condition and raw_condition != window.condition_id.lower():
            raise EvidenceError("raw fill condition conflicts with token mapping")
        shares, cash = integer(maker_amount, "maker_amount"), integer(taker_amount, "cash")
        if not shares or not cash:
            raise EvidenceError("target sale has a zero amount")
        events.append({
            "type": "maker_sell", "block_number": int(block), "timestamp": int(timestamp),
            "log_index": int(log_index), "tx_hash": str(tx_hash), "order_hash": str(order_hash),
            "condition_id": window.condition_id.lower(), "token": str(maker_asset),
            "side": "up" if side_up else "down", "shares_base": str(shares),
            "cash_delta_base": str(cash), "fee_base": "0", "maker": str(maker),
            "taker": str(taker), "raw_condition_id": raw_condition,
            "raw_outcome_index": int(outcome_index), "venue": "clob_v2",
        })
    return events


def redemption_events(rows: Sequence[tuple], conditions: set[str],
                      wallet: str) -> list[dict[str, object]]:
    for row in rows:
        (block, timestamp, log_index, tx_hash, redeemer, condition, collateral,
         parent, index_sets, payout) = row
        condition, collateral = str(condition).lower(), str(collateral).lower()
        if str(redeemer) != wallet or condition not in conditions:
            raise EvidenceError("redemption row escaped the exact target join")
        del block, timestamp, log_index, tx_hash, collateral, parent, index_sets, payout
        raise EvidenceError(
            "target redemption needs exact token-consumption accounting even at zero payout"
        )
    return []
