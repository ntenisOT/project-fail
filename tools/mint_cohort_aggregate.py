#!/usr/bin/env python3
"""Atomically aggregate the fixed mint-sibling ledger grid for falsification."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Mapping, Sequence, TypedDict

from tools.mint_accounting_inputs import (
    EvidenceError,
    WALLET_RE,
    canonical,
    digest,
    hash_file,
    integer,
    load_json,
    revision,
    sha256,
    signed_integer,
    validated_revision_manifest,
)


COHORT_SCHEMA = "project-fail-mint-sibling-cohort-proof-v1"
LEDGER_SCHEMA = "project-fail-mint-observed-accounting-v2"
SCHEMA = "project-fail-mint-sibling-falsification-v1"
UNIVERSE_KEYS = ("asset", "slug", "start", "condition_id", "up_token", "down_token")
COMMON_INPUTS = ("candidate", "attribution", "receipt_cache")


class WindowMetric(TypedDict):
    split_base: int
    sale_cash_base: int
    merge_base: int
    maker_sell_count: int
    sold_up_base: int
    sold_down_base: int
    both_outcomes_sold: bool
    terminal_pnl_base: int
    residual_zero_floor_pnl_base: int
    rebate_endpoint_base: object


class WindowAggregate(TypedDict):
    condition_id: str
    slug: str
    start: int
    wallets: int
    profitable_wallets_terminal_ex_rebate: int
    both_outcomes_sold_wallets: int
    terminal_pnl_base: int
    residual_zero_floor_pnl_base: int


class WalletAggregate(TypedDict):
    wallet: str
    ledger_sha256: str
    terminal_pnl_base_ex_rebate: str
    residual_zero_floor_pnl_base_ex_rebate: str
    profitable_windows_terminal_ex_rebate: int
    both_outcomes_sold_windows: int
    maker_sell_count: int
    split_principal_base: str
    sale_cash_base: str
    merge_return_base: str
    rebate_endpoint_base: str | None
    capital: object


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{field} must be an object")
    return value


def _rows(value: object, field: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise EvidenceError(f"{field} must be a list of objects")
    return list(value)


def _universe(cohort: Mapping[str, object]) -> dict[str, dict[str, object]]:
    rows = _rows(cohort.get("outcome_free_universe"), "cohort universe")
    universe: dict[str, dict[str, object]] = {}
    for row in rows:
        if any(key not in row for key in UNIVERSE_KEYS):
            raise EvidenceError("cohort universe row is incomplete")
        projection = {key: row[key] for key in UNIVERSE_KEYS}
        condition = str(projection["condition_id"] or "").lower()
        projection["condition_id"] = condition
        if not condition or condition in universe:
            raise EvidenceError("cohort universe conditions are empty or duplicated")
        universe[condition] = projection
    return universe


def _input_hash(inputs: Mapping[str, object], name: str) -> str:
    row = _object(inputs.get(name), f"ledger input {name}")
    return digest(str(row.get("sha256") or ""), f"ledger input {name} SHA-256")


def _ledger_window(
    row: Mapping[str, object], expected: Mapping[str, object],
) -> WindowMetric:
    if (str(row.get("slug") or "") != str(expected.get("slug") or "")
            or integer(row.get("start"), "ledger window start") != integer(
                expected.get("start"), "cohort window start"
            )):
        raise EvidenceError("ledger window differs from the fixed cohort universe")
    winner = str(row.get("winner") or "")
    if winner not in {"up", "down"}:
        raise EvidenceError("ledger window lacks an authoritative binary winner")
    split = integer(row.get("split_base"), "window split")
    sold_up = integer(row.get("sold_up_base"), "window sold up")
    sold_down = integer(row.get("sold_down_base"), "window sold down")
    terminal = signed_integer(row.get("contractual_terminal_pnl_base"), "window terminal")
    floor = signed_integer(
        row.get("contractual_pair_recovery_residual_zero_floor_pnl_base"),
        "window residual floor",
    )
    return {
        "split_base": split,
        "sale_cash_base": integer(row.get("sale_cash_base"), "window sale cash"),
        "merge_base": integer(row.get("merge_base"), "window merge"),
        "maker_sell_count": integer(row.get("maker_sell_count"), "maker sell count"),
        "sold_up_base": sold_up,
        "sold_down_base": sold_down,
        "both_outcomes_sold": sold_up > 0 and sold_down > 0,
        "terminal_pnl_base": terminal,
        "residual_zero_floor_pnl_base": floor,
        "rebate_endpoint_base": row.get("rebate_endpoint_base"),
    }


def aggregate(
    cohort: Mapping[str, object],
    ledgers: Sequence[tuple[str, Mapping[str, object], str]],
    expected_revision: Mapping[str, object],
) -> dict[str, object]:
    if cohort.get("schema") != COHORT_SCHEMA:
        raise EvidenceError("unsupported mint sibling cohort proof")
    if validated_revision_manifest(cohort.get("revision")) != dict(expected_revision):
        raise EvidenceError("cohort proof revision differs from the aggregate revision")
    raw_wallets = cohort.get("wallets")
    counts = _object(cohort.get("counts"), "cohort counts")
    if (not isinstance(raw_wallets, list)
            or any(not isinstance(wallet, str) or not WALLET_RE.fullmatch(wallet)
                   for wallet in raw_wallets)):
        raise EvidenceError("cohort wallets are malformed or duplicated")
    wallets = [str(wallet) for wallet in raw_wallets]
    if (wallets != sorted(wallets) or len(set(wallets)) != len(wallets)
            or len(wallets) != integer(counts.get("wallets"), "cohort wallets")):
        raise EvidenceError("cohort wallets are malformed or duplicated")
    universe = _universe(cohort)
    if (len(universe) != integer(counts.get("windows"), "cohort windows")
            or len(wallets) * len(universe) != integer(
                counts.get("wallet_windows"), "cohort wallet-windows"
            )):
        raise EvidenceError("cohort proof is not its declared fixed grid")
    cohort_sources = _object(cohort.get("source_sha256"), "cohort source hashes")
    expected_sources = {
        name: digest(str(cohort_sources.get(name) or ""), f"cohort {name} SHA-256")
        for name in COMMON_INPUTS
    }
    ledger_by_wallet: dict[str, tuple[Mapping[str, object], str]] = {}
    for wallet, ledger, ledger_sha in ledgers:
        if wallet in ledger_by_wallet:
            raise EvidenceError("duplicate ledger wallet")
        ledger_by_wallet[wallet] = (ledger, digest(ledger_sha, "ledger SHA-256"))
    if set(ledger_by_wallet) != set(wallets):
        raise EvidenceError("ledger set does not cover the exact frozen wallet cohort")

    outcome_sha: str | None = None
    rebate_modes: set[bool] = set()
    wallet_rows: list[WalletAggregate] = []
    window_accumulator: dict[str, WindowAggregate] = {
        condition: {
            "condition_id": condition,
            "slug": str(expected["slug"]),
            "start": integer(expected["start"], "cohort window start"),
            "wallets": 0,
            "profitable_wallets_terminal_ex_rebate": 0,
            "both_outcomes_sold_wallets": 0,
            "terminal_pnl_base": 0,
            "residual_zero_floor_pnl_base": 0,
        }
        for condition, expected in universe.items()
    }
    for wallet in wallets:
        ledger, ledger_sha = ledger_by_wallet[wallet]
        if ledger.get("schema") != LEDGER_SCHEMA:
            raise EvidenceError("unsupported mint accounting ledger")
        if validated_revision_manifest(ledger.get("revision")) != dict(expected_revision):
            raise EvidenceError("ledger revision differs from the aggregate revision")
        scope = _object(ledger.get("scope"), "ledger scope")
        if (scope.get("wallet") != wallet or scope.get("complete_wallet") is not False
                or scope.get("cash_realized") is not False):
            raise EvidenceError("ledger scope does not match the fixed observed-ledger basis")
        inputs = _object(ledger.get("inputs"), "ledger inputs")
        if any(_input_hash(inputs, name) != expected_sources[name] for name in COMMON_INPUTS):
            raise EvidenceError("ledger inputs differ from the cohort proof sources")
        current_outcome_sha = _input_hash(inputs, "outcomes")
        if outcome_sha is None:
            outcome_sha = current_outcome_sha
        elif outcome_sha != current_outcome_sha:
            raise EvidenceError("cohort ledgers do not share one payout artifact")
        has_rebate = "rebate" in inputs
        rebate_modes.add(has_rebate)
        mapping_rows = _rows(ledger.get("market_mapping"), "ledger market mapping")
        projection: dict[str, dict[str, object]] = {}
        for raw in mapping_rows:
            if any(key not in raw for key in UNIVERSE_KEYS):
                raise EvidenceError("ledger market mapping row is incomplete")
            row = {key: raw[key] for key in UNIVERSE_KEYS}
            condition = str(row["condition_id"] or "").lower()
            row["condition_id"] = condition
            if condition in projection:
                raise EvidenceError("ledger market mapping duplicates a condition")
            projection[condition] = row
        if projection != universe:
            raise EvidenceError("ledger market mapping differs from the cohort universe")
        ledger_windows = _rows(ledger.get("windows"), "ledger windows")
        validated_windows: dict[str, WindowMetric] = {}
        for raw in ledger_windows:
            condition = str(raw.get("condition_id") or "").lower()
            if condition not in universe or condition in validated_windows:
                raise EvidenceError("ledger windows escape or duplicate the cohort universe")
            validated_windows[condition] = _ledger_window(raw, universe[condition])
        if set(validated_windows) != set(universe):
            raise EvidenceError("ledger does not cover every cohort window")
        coverage = _object(ledger.get("source_coverage"), "ledger source coverage")
        if integer(coverage.get("target_trade_involvement_rows"), "target involvement") != integer(
            coverage.get("accepted_fee_zero_v2_maker_sale_rows"), "accepted maker sales"
        ):
            raise EvidenceError("ledger target involvement was not exhaustively normalized")
        totals = _object(ledger.get("totals"), "ledger totals")
        overlay = _object(totals.get("rebate_overlay"), "ledger rebate overlay")
        endpoint = overlay.get("endpoint_base")
        if has_rebate:
            rebate_endpoint = integer(endpoint, "rebate endpoint")
        elif endpoint is None:
            rebate_endpoint = None
        else:
            raise EvidenceError("ledger rebate input and endpoint disagree")
        if any((row["rebate_endpoint_base"] is None) == has_rebate
               for row in validated_windows.values()):
            raise EvidenceError("ledger window rebate coverage is incomplete")
        if has_rebate and rebate_endpoint != sum(
            integer(row["rebate_endpoint_base"], "window rebate endpoint")
            for row in validated_windows.values()
        ):
            raise EvidenceError("ledger rebate endpoint does not reconcile to its windows")
        terminal = signed_integer(totals.get("contractual_terminal_pnl_base"), "terminal")
        floor = signed_integer(
            totals.get("contractual_pair_recovery_residual_zero_floor_pnl_base"), "floor"
        )
        if terminal != sum(row["terminal_pnl_base"] for row in validated_windows.values()):
            raise EvidenceError("ledger terminal total does not reconcile to its windows")
        if floor != sum(
            row["residual_zero_floor_pnl_base"] for row in validated_windows.values()
        ):
            raise EvidenceError("ledger residual floor does not reconcile to its windows")
        split_total = integer(totals.get("split_principal_base"), "split principal")
        sale_cash_total = integer(totals.get("sale_cash_base"), "sale cash")
        merge_total = integer(totals.get("merge_return_base"), "merge return")
        if (split_total != sum(row["split_base"] for row in validated_windows.values())
                or sale_cash_total != sum(
                    row["sale_cash_base"] for row in validated_windows.values()
                )
                or merge_total != sum(row["merge_base"] for row in validated_windows.values())):
            raise EvidenceError("ledger cash principals do not reconcile to its windows")
        both_sides = sum(row["both_outcomes_sold"] for row in validated_windows.values())
        profitable_windows = sum(row["terminal_pnl_base"] > 0 for row in validated_windows.values())
        maker_sells = sum(row["maker_sell_count"] for row in validated_windows.values())
        ledger_counts = _object(ledger.get("counts"), "ledger counts")
        if (integer(ledger_counts.get("markets"), "ledger markets") != len(universe)
                or integer(ledger_counts.get("maker_sells"), "ledger maker sells") != maker_sells):
            raise EvidenceError("ledger counts do not reconcile to its windows")
        wallet_rows.append({
            "wallet": wallet,
            "ledger_sha256": ledger_sha,
            "terminal_pnl_base_ex_rebate": str(terminal),
            "residual_zero_floor_pnl_base_ex_rebate": str(floor),
            "profitable_windows_terminal_ex_rebate": profitable_windows,
            "both_outcomes_sold_windows": both_sides,
            "maker_sell_count": maker_sells,
            "split_principal_base": str(split_total),
            "sale_cash_base": str(sale_cash_total),
            "merge_return_base": str(merge_total),
            "rebate_endpoint_base": (
                None if rebate_endpoint is None else str(rebate_endpoint)
            ),
            "capital": ledger.get("capital"),
        })
        for condition, window_metric in validated_windows.items():
            aggregate_row = window_accumulator[condition]
            aggregate_row["wallets"] += 1
            aggregate_row["profitable_wallets_terminal_ex_rebate"] += int(
                window_metric["terminal_pnl_base"] > 0
            )
            aggregate_row["both_outcomes_sold_wallets"] += int(
                window_metric["both_outcomes_sold"]
            )
            aggregate_row["terminal_pnl_base"] += window_metric["terminal_pnl_base"]
            aggregate_row["residual_zero_floor_pnl_base"] += window_metric[
                "residual_zero_floor_pnl_base"
            ]
    if len(rebate_modes) != 1:
        raise EvidenceError("cohort ledgers mix absent and complete rebate evidence")
    rebate_complete = rebate_modes == {True}
    terminal_total = sum(int(row["terminal_pnl_base_ex_rebate"]) for row in wallet_rows)
    floor_total = sum(int(row["residual_zero_floor_pnl_base_ex_rebate"])
                      for row in wallet_rows)
    rebate_total = (
        sum(int(str(row["rebate_endpoint_base"])) for row in wallet_rows)
        if rebate_complete else None
    )
    split_total = sum(int(row["split_principal_base"]) for row in wallet_rows)
    sale_cash_total = sum(int(row["sale_cash_base"]) for row in wallet_rows)
    merge_total = sum(int(row["merge_return_base"]) for row in wallet_rows)
    window_rows: list[dict[str, object]] = []
    for aggregate_metric in sorted(
        window_accumulator.values(),
        key=lambda item: (item["start"], item["condition_id"]),
    ):
        window_rows.append({
            **aggregate_metric,
            "terminal_pnl_base": str(aggregate_metric["terminal_pnl_base"]),
            "residual_zero_floor_pnl_base": str(
                aggregate_metric["residual_zero_floor_pnl_base"]
            ),
        })
    return {
        "schema": SCHEMA,
        "basis": "fixed_10x31_observed_ledgers_with_contractual_terminal_marks",
        "counts": {"wallets": len(wallets), "windows": len(universe),
                   "wallet_windows": len(wallets) * len(universe)},
        "common_inputs": {**expected_sources, "outcomes": outcome_sha},
        "rebate_evidence": "complete" if rebate_complete else "absent",
        "wallets": wallet_rows,
        "windows": window_rows,
        "totals": {
            "terminal_pnl_base_ex_rebate": str(terminal_total),
            "residual_zero_floor_pnl_base_ex_rebate": str(floor_total),
            "split_principal_base": str(split_total),
            "sale_cash_base": str(sale_cash_total),
            "merge_return_base": str(merge_total),
            "profitable_wallets_terminal_ex_rebate": sum(
                int(row["terminal_pnl_base_ex_rebate"]) > 0 for row in wallet_rows
            ),
            "profitable_wallet_windows_terminal_ex_rebate": sum(
                row["profitable_windows_terminal_ex_rebate"] for row in wallet_rows
            ),
            "both_outcomes_sold_wallet_windows": sum(
                row["both_outcomes_sold_windows"] for row in wallet_rows
            ),
            "rebate_endpoint_base": None if rebate_total is None else str(rebate_total),
        },
        "capital_policy": (
            "per-wallet paths only; no cross-address netting or cohort ROI because "
            "operator independence is unproven"
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--cohort-sha256", required=True)
    parser.add_argument("--ledger-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    try:
        start_revision = revision(repo)
        cohort_sha = digest(args.cohort_sha256, "cohort SHA-256")
        cohort = load_json(args.cohort, cohort_sha, COHORT_SCHEMA)
        raw_wallets = cohort.get("wallets")
        if (not isinstance(raw_wallets, list)
                or any(not isinstance(wallet, str) for wallet in raw_wallets)):
            raise EvidenceError("cohort proof lacks wallets")
        wallets = [str(wallet) for wallet in raw_wallets]
        loaded: list[tuple[str, Mapping[str, object], str]] = []
        paths: dict[str, Path] = {}
        hashes: dict[str, str] = {}
        for raw_wallet in wallets:
            wallet = str(raw_wallet)
            path = args.ledger_dir / f"{wallet}.json"
            ledger_sha = hash_file(path)
            ledger = load_json(path, ledger_sha, LEDGER_SCHEMA)
            paths[wallet], hashes[wallet] = path, ledger_sha
            loaded.append((wallet, ledger, ledger_sha))
        payload = aggregate(cohort, loaded, start_revision)
        payload["inputs"] = {
            "cohort": {"path": str(args.cohort), "sha256": cohort_sha},
            "ledgers": [
                {"wallet": wallet, "path": str(paths[wallet]), "sha256": hashes[wallet]}
                for wallet in wallets
            ],
        }
        payload["revision"] = start_revision
        if (hash_file(args.cohort) != cohort_sha
                or any(hash_file(path) != hashes[wallet] for wallet, path in paths.items())
                or revision(repo) != start_revision):
            raise EvidenceError("an aggregate input or source revision changed during production")
        raw = canonical(payload) + b"\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as handle:
            handle.write(raw)
    except (EvidenceError, OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    output_counts = _object(payload.get("counts"), "aggregate counts")
    print(
        f"wrote {args.output} sha256={sha256(raw)} "
        f"wallet_windows={output_counts['wallet_windows']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
