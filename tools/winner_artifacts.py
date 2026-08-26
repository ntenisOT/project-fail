"""Validation for frozen winner cohorts, fills, markets, and Gamma regimes."""

from __future__ import annotations

import json
import math
import pathlib
import re
from collections.abc import Mapping
from dataclasses import dataclass

from tools.crossvenue_dataset import JoinIntegrityError, count_value, file_sha256


COHORT_SCHEMA = "project-fail-frozen-wallet-cohort-v1"
GAMMA_SCHEMA = "project-fail-gamma-resolution-regimes-v1"
REQUIRED = frozenset({"cohort", "wallet_fills", "markets", "gamma"})
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
WALLET_RE = re.compile(r"^0x[0-9a-f]{40}$")
TX_RE = re.compile(r"^0x[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[0-9]+$")
CONDITION_RE = re.compile(r"^0x[0-9a-f]{64}$")


@dataclass(frozen=True)
class Market:
    slug: str
    start: int
    up_token: str
    down_token: str


def _object(path: pathlib.Path, kind: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JoinIntegrityError(f"invalid {kind}: {exc}") from exc
    if not isinstance(value, dict):
        raise JoinIntegrityError(f"{kind} is not an object")
    return value


def _jsonl(path: pathlib.Path, kind: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise JoinIntegrityError(f"{kind} row {line_number} is not an object")
                rows.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise JoinIntegrityError(f"invalid {kind}: {exc}") from exc
    if not rows:
        raise JoinIntegrityError(f"{kind} has no rows")
    return rows


def _canonical_int(row: Mapping[str, object], field: str) -> int:
    value = row.get(field)
    if type(value) is not int or value < 0:
        raise JoinIntegrityError(f"{field} must be a non-negative JSON integer")
    return value


def _number(row: Mapping[str, object], field: str) -> float:
    try:
        value = float(str(row.get(field)))
    except ValueError as exc:
        raise JoinIntegrityError(f"invalid {field}") from exc
    if not math.isfinite(value):
        raise JoinIntegrityError(f"non-finite {field}")
    return value


def _cohort(path: pathlib.Path, overlap_start_s: int) -> tuple[set[str], dict[str, object]]:
    value = _object(path, "frozen cohort")
    schema = value.get("schema")
    if schema not in (None, COHORT_SCHEMA):
        raise JoinIntegrityError("unsupported frozen cohort schema")
    period = value.get("period")
    wallets_value = value.get("wallets")
    selection = str(value.get("selection") or "")
    if not isinstance(period, dict) or not isinstance(wallets_value, list):
        raise JoinIntegrityError("frozen cohort lacks period or wallets")
    wallets = [str(wallet) for wallet in wallets_value]
    wallet_set = set(wallets)
    if (not wallets or len(wallet_set) != len(wallets)
            or any(not WALLET_RE.fullmatch(wallet) for wallet in wallets)
            or "frozen" not in selection.lower()):
        raise JoinIntegrityError("cohort wallets or frozen selection are invalid")
    start = count_value(period.get("start"), "cohort period start")
    end = count_value(period.get("end"), "cohort period end")
    discovery_end = count_value(value.get("discovery_end"), "discovery_end")
    holdout_start = count_value(value.get("holdout_start"), "holdout_start")
    if (any(boundary % 300 for boundary in (start, end, discovery_end, holdout_start))
            or not start <= discovery_end < holdout_start <= end
            or end + 300 > overlap_start_s):
        raise JoinIntegrityError("frozen cohort period leaks into joined capture")
    selection_sources = value.get("selection_sources")
    if (selection_sources is not None
            and (not isinstance(selection_sources, dict)
                 or set(selection_sources) != wallet_set
                 or any(not isinstance(sources, list) or not sources
                        or any(not isinstance(source, str) or not source
                               for source in sources)
                        for sources in selection_sources.values()))):
        raise JoinIntegrityError("cohort selection-source coverage is inconsistent")
    return wallet_set, {
        "schema": COHORT_SCHEMA if schema else "legacy-lifecycle-frozen-v1",
        "wallets": len(wallets), "period_start": start, "period_end": end,
        "selection": selection,
    }


def _markets(path: pathlib.Path, first: int, last: int) -> dict[str, Market]:
    markets: dict[str, Market] = {}
    condition_ids: set[str] = set()
    token_ids: set[str] = set()
    for row in _jsonl(path, "market mapping"):
        slug = str(row.get("slug") or "")
        asset = str(row.get("asset") or "").lower()
        start = _canonical_int(row, "start")
        condition = str(row.get("condition_id") or "").lower()
        up_token, down_token = str(row.get("up_token") or ""), str(row.get("down_token") or "")
        winner = row.get("winner_up")
        if (slug in markets or asset != "btc" or slug != f"btc-updown-5m-{start}"
                or start % 300 or not CONDITION_RE.fullmatch(condition)
                or not TOKEN_RE.fullmatch(up_token) or not TOKEN_RE.fullmatch(down_token)
                or condition in condition_ids or up_token in token_ids
                or down_token in token_ids or up_token == down_token
                or type(winner) is not int or winner not in (0, 1)):
            raise JoinIntegrityError(f"invalid market mapping: {slug}")
        markets[slug] = Market(slug, start, up_token, down_token)
        condition_ids.add(condition)
        token_ids.update((up_token, down_token))
    expected = set(range(first, last + 1, 300))
    if {market.start for market in markets.values()} != expected:
        raise JoinIntegrityError("market mappings do not cover exact complete overlap")
    return markets


def _gamma(path: pathlib.Path, market_slugs: set[str]) -> dict[str, object]:
    value = _object(path, "Gamma resolution regimes")
    rows = value.get("rows")
    if value.get("schema") != GAMMA_SCHEMA or not isinstance(rows, list):
        raise JoinIntegrityError("unsupported Gamma resolution-regime schema")
    slugs: set[str] = set()
    lookbacks: set[int] = set()
    sources: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise JoinIntegrityError(f"invalid Gamma regime row {index}")
        slug = str(row.get("slug") or "")
        source = str(row.get("resolution_source") or "")
        config_id = str(row.get("config_id") or "")
        lookback = count_value(row.get("lookback_s"), "Gamma lookback_s")
        opening = _number(row, "price_to_beat")
        final = _number(row, "final_price")
        if (not slug or slug in slugs or not source or not config_id
                or lookback == 0 or opening <= 0 or final <= 0):
            raise JoinIntegrityError(f"incomplete Gamma regime row {index}")
        slugs.add(slug)
        sources.add(source)
        lookbacks.add(lookback)
    if slugs != market_slugs:
        raise JoinIntegrityError("Gamma regimes do not cover exact market mappings")
    return {
        "markets": len(slugs), "lookback_s": sorted(lookbacks),
        "resolution_sources": sorted(sources),
        "policy": "per_market_from_gamma_never_global_default",
    }


def _fills(
    path: pathlib.Path, wallets: set[str], markets: Mapping[str, Market],
) -> dict[str, object]:
    active_wallets: set[str] = set()
    active_markets: set[str] = set()
    identities: set[tuple[object, ...]] = set()
    missing_tx = maker = taker = 0
    required = {
        "wallet", "slug", "token", "side", "block_number", "log_index",
        "block_ts", "role", "size", "price", "fee", "tx_hash",
    }
    rows = _jsonl(path, "wallet fills")
    for line_number, row in enumerate(rows, 1):
        if not required <= set(row):
            raise JoinIntegrityError(f"wallet fill row {line_number} lacks required fields")
        wallet, slug = str(row["wallet"]), str(row["slug"])
        token, role = str(row["token"]), str(row["role"])
        side = row["side"]
        market = markets.get(slug)
        block = _canonical_int(row, "block_number")
        log_index = _canonical_int(row, "log_index")
        block_ts = _canonical_int(row, "block_ts")
        size, price, fee = _number(row, "size"), _number(row, "price"), _number(row, "fee")
        tx_hash = row["tx_hash"]
        if wallet not in wallets:
            raise JoinIntegrityError(f"wallet fill outside frozen cohort: {wallet}")
        if (market is None or type(side) is not int or side not in (0, 1)
                or not TOKEN_RE.fullmatch(token)
                or token != (market.up_token if side else market.down_token)
                or not market.start <= block_ts < market.start + 300):
            raise JoinIntegrityError(f"wallet fill market mapping is invalid at row {line_number}")
        if role not in {"maker", "taker"} or size <= 0 or not 0 <= price <= 1 or fee < 0:
            raise JoinIntegrityError(f"wallet fill economics are invalid at row {line_number}")
        if tx_hash in (None, ""):
            missing_tx += 1
        elif not isinstance(tx_hash, str) or not TX_RE.fullmatch(tx_hash):
            raise JoinIntegrityError(f"wallet fill tx_hash is invalid at row {line_number}")
        identity = (wallet, slug, token, block, log_index)
        if identity in identities:
            raise JoinIntegrityError(f"duplicate wallet fill at row {line_number}")
        identities.add(identity)
        active_wallets.add(wallet)
        active_markets.add(slug)
        maker += int(role == "maker")
        taker += int(role == "taker")
    return {
        "rows": len(rows), "maker_rows": maker, "taker_rows": taker,
        "tx_hash_present": len(rows) - missing_tx, "tx_hash_missing": missing_tx,
        "active_wallets": len(active_wallets), "inactive_wallets": len(wallets - active_wallets),
        "active_markets": len(active_markets), "inactive_markets": len(markets) - len(active_markets),
        "coverage_policy": "fills_must_be_subset; zero-fill wallets_and_markets_are_inactive",
    }


def validate_artifacts(
    artifacts: Mapping[str, pathlib.Path], *, overlap_start_s: int,
    first_window: int, last_window: int,
) -> dict[str, object]:
    if not REQUIRED <= set(artifacts):
        raise JoinIntegrityError(
            f"required passive artifacts are missing: {sorted(REQUIRED - set(artifacts))}"
        )
    output: dict[str, object] = {}
    for name, path in sorted(artifacts.items()):
        if not NAME_RE.fullmatch(name):
            raise JoinIntegrityError(f"invalid artifact name: {name}")
        try:
            size, digest = path.stat().st_size, file_sha256(path)
        except OSError as exc:
            raise JoinIntegrityError(f"artifact is missing: {path}") from exc
        if size == 0:
            raise JoinIntegrityError(f"artifact is empty: {path}")
        output[name] = {"path": path.as_posix(), "sha256": digest, "bytes": size}
    wallets, cohort = _cohort(artifacts["cohort"], overlap_start_s)
    markets = _markets(artifacts["markets"], first_window, last_window)
    validations = {
        "cohort": cohort,
        "markets": {"rows": len(markets), "coverage": "exact_complete_overlap"},
        "gamma": _gamma(artifacts["gamma"], set(markets)),
        "wallet_fills": _fills(artifacts["wallet_fills"], wallets, markets),
    }
    for name, validation in validations.items():
        row = output[name]
        assert isinstance(row, dict)
        row["validation"] = validation
    for name, path in artifacts.items():
        row = output[name]
        assert isinstance(row, dict)
        try:
            unchanged = path.stat().st_size == row["bytes"] and file_sha256(path) == row["sha256"]
        except OSError as exc:
            raise JoinIntegrityError(f"artifact disappeared during validation: {path}") from exc
        if not unchanged:
            raise JoinIntegrityError(f"artifact changed during validation: {path}")
    return output
