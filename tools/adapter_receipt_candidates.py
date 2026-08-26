"""Export bounded adapter receipt candidates without fetching Polygon receipts."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence, cast

import clickhouse_connect  # type: ignore[import-untyped]

from tools.clickhouse_forensics import (
    LIFECYCLE_LOOKBACK_S,
    LIFECYCLE_TAIL_S,
    SETTINGS,
    window_external_data,
)
from tools.crossvenue_dataset import JoinIntegrityError
from tools.market_windows import ASSET_PREFIX, ResolvedWindow, fetch_gamma_window, load_window_cache
from tools.top_setters import DEFAULT_CACHE, parse_timestamp
import tools.winner_artifacts as winner_artifacts

SCHEMA = "project-fail-adapter-receipt-candidates-v1"
QUERY_SCHEMA = "project-fail-adapter-receipt-candidate-query-v1"
CHAIN_ID = 137
OLD_FACTORY = "0xada100874d00e3331d00f2007a9c336a65009718"
STANDARD_ADAPTER = "0xada100db00ca00073811820692005400218fce1f"
NEG_RISK_ADAPTER = "0xada2005600dec949baf300f4c6120000bdb6eaab"
ADAPTER_KIND = {
    OLD_FACTORY: "legacy_clob_factory", STANDARD_ADAPTER: "standard",
    NEG_RISK_ADAPTER: "neg_risk",
}
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
HARD_MAX_CANDIDATES = 10_000
EXPECTED_COLUMNS = {
    "splits_merges": {
        "block_number": "UInt64", "block_timestamp": "DateTime64(0, 'UTC')",
        "tx_hash": "FixedString(66)", "log_index": "UInt16",
        "op": "Enum8('split' = 1, 'merge' = 2)", "stakeholder": "LowCardinality(String)",
        "condition_id": "String", "amount": "UInt256",
    },
    "trade_history": {
        "block_timestamp": "DateTime64(0, 'UTC')", "tx_hash": "FixedString(66)",
        "maker_asset_id": "String", "taker_asset_id": "String",
        "maker_amount_filled": "UInt256", "taker_amount_filled": "UInt256",
    },
}
class ExportError(ValueError):
    """The requested interval or source data cannot produce a safe artifact."""

def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")

def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()

def _hex_digest(value: str, field: str) -> str:
    result = value.lower()
    if not HEX64_RE.fullmatch(result):
        raise ExportError(f"{field} must be 64 lowercase hex digits")
    return result

def _load_cohort(path: Path, expected_sha256: str, first_window: int) -> tuple[list[str], str]:
    raw = path.read_bytes()
    digest = _sha256(raw)
    if digest != _hex_digest(expected_sha256, "cohort_sha256"):
        raise ExportError("frozen cohort SHA-256 mismatch")
    try:
        value = json.loads(raw)
        validated, _ = winner_artifacts._cohort(path, first_window)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError("frozen cohort is not valid UTF-8 JSON") from exc
    except JoinIntegrityError as exc:
        raise ExportError(f"invalid frozen cohort: {exc}") from exc
    if not isinstance(value, Mapping) or not isinstance(value.get("wallets"), list):
        raise ExportError("frozen cohort lacks an ordered wallet list")
    wallets = [str(wallet).lower() for wallet in cast(list[object], value["wallets"])]
    if set(wallets) != validated:
        raise ExportError("validated cohort differs from its ordered wallet list")
    return wallets, digest

def _validate_windows(windows: Sequence[ResolvedWindow], start: int, end: int) -> None:
    expected = list(range(start, end + 1, 300))
    if [window.start for window in windows] != expected:
        raise ExportError("resolved BTC mapping does not cover the exact interval")
    conditions: set[str] = set()
    tokens: set[str] = set()
    for window in windows:
        window.validate()
        if window.asset != "btc" or window.slug != f"{ASSET_PREFIX['btc']}-{window.start}":
            raise ExportError("non-BTC or inconsistent market mapping")
        if window.condition_id != window.condition_id.lower():
            raise ExportError("market condition IDs must be canonical lowercase hex")
        if window.condition_id in conditions or {window.up_token, window.down_token} & tokens:
            raise ExportError("duplicate condition or token in market mapping")
        conditions.add(window.condition_id)
        tokens.update((window.up_token, window.down_token))

def _resolve_windows(
    start: int, end: int, cache_path: Path, *, workers: int, fetch_missing: bool,
) -> tuple[list[ResolvedWindow], int]:
    cache = load_window_cache(cache_path)
    wanted = [(timestamp, f"{ASSET_PREFIX['btc']}-{timestamp}")
              for timestamp in range(start, end + 1, 300)]
    absent = [timestamp for timestamp, slug in wanted if slug not in cache]
    if absent and not fetch_missing:
        raise ExportError(f"{len(absent)} resolved windows are absent from the supplied cache")
    if absent:
        with ThreadPoolExecutor(max_workers=min(workers, len(absent))) as pool:
            fetched = list(pool.map(lambda timestamp: fetch_gamma_window("btc", timestamp), absent))
        if any(window is None for window in fetched):
            raise ExportError("Gamma returned an unresolved BTC window")
        for window in fetched:
            assert window is not None
            cache[window.slug] = window
    windows = [cache[slug] for _, slug in wanted]
    _validate_windows(windows, start, end)
    return windows, len(absent)

def _mapping_rows(windows: Sequence[ResolvedWindow]) -> list[dict[str, object]]:
    return [{
        "asset": window.asset, "condition_id": window.condition_id.lower(),
        "down_token": window.down_token, "slug": window.slug, "start": window.start,
        "up_token": window.up_token, "winner_up": window.winner_up,
    } for window in windows]

def _validate_schema(client: Any) -> None:
    for table, required in EXPECTED_COLUMNS.items():
        actual = {str(row[0]): str(row[1]) for row in client.query(f"DESCRIBE TABLE {table}").result_rows}
        if any(actual.get(column) != kind for column, kind in required.items()):
            raise ExportError(f"ClickHouse schema mismatch for {table}")
def _candidate_sql(t0: int, t1: int) -> str:
    adapters = ",".join(f"'{adapter}'" for adapter in ADAPTER_KIND)
    return f"""
    WITH clob_legs AS (
      SELECT th.tx_hash AS clob_tx, w.condition_id AS clob_condition, w.token AS clob_token,
             if(th.maker_asset_id='0',toInt256(th.taker_amount_filled),
                -toInt256(th.maker_amount_filled)) AS clob_delta
      FROM trade_history th INNER JOIN set_windows w
        ON if(th.maker_asset_id='0',th.taker_asset_id,th.maker_asset_id)=w.token
      WHERE th.block_timestamp>=toDateTime({t0}) AND th.block_timestamp<toDateTime({t1})
        AND ((th.maker_asset_id='0' AND th.taker_asset_id!='0')
             OR (th.taker_asset_id='0' AND th.maker_asset_id!='0'))
    ), clob_nets AS (
      SELECT clob_tx, clob_condition, clob_token, sum(clob_delta) AS clob_net
      FROM clob_legs GROUP BY clob_tx, clob_condition, clob_token
    ), clob_token_ops AS (
      SELECT clob_tx, clob_condition, clob_token,
             if(clob_net>0,'split','merge') AS clob_op,
             toUInt256(abs(clob_net)) AS clob_amount FROM clob_nets WHERE clob_net!=0
    ), clob_ops AS (
      SELECT clob_tx, clob_condition, clob_op, clob_amount, toUInt8(1) AS matched
      FROM clob_token_ops GROUP BY clob_tx, clob_condition, clob_op, clob_amount
      HAVING uniqExact(clob_token)=2
    )
    SELECT sm.block_number, sm.log_index, toUInt32(sm.block_timestamp),
           lower(toString(sm.tx_hash)), lower(sm.condition_id), toString(sm.op),
           lower(sm.stakeholder), toString(sm.amount),
           ifNull(co.matched,0)=1 AS same_tx_clob
    FROM splits_merges sm
    INNER JOIN (SELECT DISTINCT condition_id FROM set_windows) w
      ON lower(sm.condition_id)=w.condition_id
    LEFT JOIN clob_ops co ON sm.tx_hash=co.clob_tx
      AND lower(sm.condition_id)=co.clob_condition AND toString(sm.op)=co.clob_op
      AND sm.amount=co.clob_amount
    WHERE sm.block_timestamp>=toDateTime({t0}) AND sm.block_timestamp<toDateTime({t1})
      AND lower(sm.stakeholder) IN ({adapters})
      AND (lower(sm.stakeholder)!='{OLD_FACTORY}' OR ifNull(co.matched,0)=0)
    ORDER BY sm.block_number, sm.log_index
    """.strip()
def _candidate_rows(
    rows: Sequence[Sequence[object]], windows: Sequence[ResolvedWindow], max_candidates: int,
) -> tuple[list[dict[str, object]], int]:
    tokens = {window.condition_id.lower(): (window.up_token, window.down_token) for window in windows}
    grouped: dict[tuple[object, ...], dict[str, object]] = {}
    output_keys: dict[tuple[str, str, str], tuple[object, ...]] = {}
    seen_source_rows: set[tuple[object, ...]] = set()
    duplicate_rows = 0
    for index, row in enumerate(rows):
        if len(row) != 9:
            raise ExportError(f"candidate query row {index} has the wrong shape")
        block, log_index, block_ts = row[0], row[1], row[2]
        tx_hash, condition, op, adapter = (str(value).lower() for value in row[3:7])
        amount_text, same_tx_clob = str(row[7]), row[8]
        if (type(block) is not int or type(log_index) is not int or type(block_ts) is not int
                or not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash)
                or condition not in tokens or op not in {"split", "merge"}
                or adapter not in ADAPTER_KIND or not amount_text.isdecimal()
                or int(amount_text) <= 0
                or not (type(same_tx_clob) is bool
                        or type(same_tx_clob) is int and same_tx_clob in (0, 1))):
            raise ExportError(f"candidate query row {index} is malformed")
        has_clob = bool(same_tx_clob)
        if adapter == OLD_FACTORY and has_clob:
            raise ExportError("old-factory CLOB-classified row escaped the SQL exclusion")
        token_ids = tokens[condition]
        identity = (tx_hash, condition, op, adapter, int(amount_text), token_ids)
        source_identity = (block, log_index, block_ts, *identity, has_clob)
        if source_identity in seen_source_rows:
            duplicate_rows += 1
            continue
        seen_source_rows.add(source_identity)
        prior = grouped.get(identity)
        if prior is not None:
            raise ExportError("candidate identity occurs at distinct source logs")
        output_key = (tx_hash, condition, op)
        if output_key in output_keys:
            raise ExportError("multiple candidates collide on the attribution output join key")
        output_keys[output_key] = identity
        grouped[identity] = {
            "adapter": adapter, "adapter_kind": ADAPTER_KIND[adapter], "amount": amount_text,
            "candidate_scope": ("old_factory_without_same_tx_clob"
                                if adapter == OLD_FACTORY else "current_adapter_lifecycle"),
            "condition_id": condition, "op": op, "same_tx_clob": has_clob,
            "source_block_number": block, "source_block_timestamp": block_ts,
            "source_log_index": log_index,
            "token_ids": [token_ids[0], token_ids[1]],
            "tx_hash": tx_hash,
        }
    candidates = sorted(grouped.values(), key=lambda row: (
        cast(int, row["source_block_number"]), cast(int, row["source_log_index"]),
        str(row["tx_hash"]),
        str(row["condition_id"]), str(row["op"]), str(row["amount"]),
    ))
    if not candidates:
        raise ExportError("candidate query returned zero candidates")
    if len(candidates) > max_candidates:
        raise ExportError(f"candidate count {len(candidates)} exceeds bound {max_candidates}")
    return candidates, duplicate_rows

def _git_head(repo: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ExportError("unable to resolve Git revision") from exc

def _write_immutable(path: Path, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return _sha256(encoded)

def run(args: argparse.Namespace) -> tuple[str, int]:
    if args.end < args.start or args.start % 300 or args.end % 300:
        raise ExportError("start/end must be an increasing five-minute-aligned interval")
    source_path = Path(__file__).resolve()
    source_hash = _sha256(source_path.read_bytes())
    cohort, cohort_hash = _load_cohort(args.cohort, args.cohort_sha256, args.start)
    windows, fetched = _resolve_windows(
        args.start, args.end, args.market_cache, workers=args.workers,
        fetch_missing=not args.no_fetch,
    )
    mapping = _mapping_rows(windows)
    mapping_hash = _sha256(_canonical(mapping))
    t0, t1 = args.start - LIFECYCLE_LOOKBACK_S, args.end + LIFECYCLE_TAIL_S
    client = clickhouse_connect.get_client(
        host="localhost", port=8123, username="copypoly", password="copypoly",
        database="copypoly",
    )
    watermarks: dict[str, int] = {}
    try:
        _validate_schema(client)
        for table in EXPECTED_COLUMNS:
            maximum = int(client.command(f"SELECT toUInt32(max(block_timestamp)) FROM {table}"))
            watermarks[table] = maximum
            if maximum < t1:
                raise ExportError(f"{table} is incomplete before lifecycle end {t1}")
        sql = _candidate_sql(t0, t1)
        raw_rows = client.query(sql, settings=SETTINGS,
                                external_data=window_external_data(windows)).result_rows
    finally:
        client.close()
    candidates, duplicate_rows = _candidate_rows(raw_rows, windows, args.max_candidates)
    query_spec = {
        "adapters": {kind: adapter for adapter, kind in ADAPTER_KIND.items()},
        "current_adapter_rule": "all mapped-condition lifecycle split/merge operations",
        "legacy_factory_rule": "exclude exact tx+condition+op+UInt256 net CLOB-flow joins",
        "lifecycle_end_exclusive": t1, "lifecycle_start": t0,
        "schema": QUERY_SCHEMA, "settings": SETTINGS, "sql": sql,
    }
    query_hash = _sha256(_canonical(query_spec))
    start_block = min(cast(int, row["source_block_number"]) for row in candidates)
    end_block = max(cast(int, row["source_block_number"]) for row in candidates) + 1
    query = {
        "candidate_query_sha256": query_hash, "chain_id": CHAIN_ID,
        "cohort_sha256": cohort_hash, "end": args.end, "end_block": end_block,
        "lifecycle_end_exclusive": t1, "lifecycle_start": t0,
        "market_mapping_sha256": mapping_hash,
        "source_watermark_unix_s": watermarks,
        "start": args.start, "start_block": start_block,
    }
    repo = source_path.parents[1]
    payload = {
        "schema": SCHEMA, "query": query, "candidate_query": query_spec,
        "cohort": cohort, "market_mapping": mapping,
        "counts": {
            "candidates": len(candidates), "cohort_wallets": len(cohort),
            "deduplicated_source_rows": duplicate_rows, "gamma_fetched_in_memory": fetched,
            "markets": len(mapping),
        },
        "generator": {"git_head": _git_head(repo), "source_sha256": source_hash},
        "limitations": [
            "no Polygon receipts or traces are fetched by this producer",
            "legacy candidates exclude only exact tx/condition/op/amount CLOB set joins",
            "current adapter candidates are lifecycle events and remain unattributed until receipts decode",
            "candidate block bounds cover emitted candidates; timestamp bounds define the source query",
        ],
        "candidates": candidates,
    }
    if _sha256(args.cohort.read_bytes()) != cohort_hash:
        raise ExportError("cohort changed during export")
    if _sha256(source_path.read_bytes()) != source_hash:
        raise ExportError("producer source changed during export")
    digest = _write_immutable(args.output, payload)
    return digest, len(candidates)

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=parse_timestamp, required=True)
    parser.add_argument("--end", type=parse_timestamp, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--cohort-sha256", required=True)
    parser.add_argument("--market-cache", type=Path, default=Path(DEFAULT_CACHE))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-candidates", type=int, default=4_000)
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 32 or not 1 <= args.max_candidates <= HARD_MAX_CANDIDATES:
        parser.error("workers must be 1..32 and max-candidates 1..10000")
    try:
        digest, count = run(args)
    except (ExportError, FileExistsError, OSError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps({"candidates": count, "output": str(args.output),
                      "output_sha256": digest}, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
