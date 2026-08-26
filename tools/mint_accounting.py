#!/usr/bin/env python3
"""Freeze exact observed-ledger accounting for a receipt-attributed mint seller."""

from __future__ import annotations

import argparse
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Mapping, Sequence

import clickhouse_connect  # type: ignore[import-untyped]

from tools.mint_accounting_clickhouse import PUSD, fill_events, redemption_events, source_rows
from tools.mint_accounting_capital import capital_diagnostics
from tools.mint_accounting_core import AccountingError, Sale, allocation_diagnostics, rational
from tools.mint_attribution_validation import attributed_events
from tools.mint_accounting_inputs import (
    EvidenceError,
    WALLET_RE,
    canonical,
    digest,
    hash_file,
    integer,
    load_json,
    mapping,
    rebates,
    revision,
    sha256,
    signed_integer,
    verify_receipts,
)
from tools.mint_accounting_outcomes import verified_winners


SCHEMA = "project-fail-mint-observed-accounting-v2"
MIN_POST_CLOSE_TAIL_S = 24 * 60 * 60


def _finality_gate(
    chain: Mapping[str, object], t1: int, events: Sequence[Mapping[str, object]],
) -> None:
    finalized_block = integer(chain.get("block_number"), "finalized payout block")
    finalized_at = integer(chain.get("block_timestamp"), "finalized payout timestamp")
    if finalized_at < t1:
        raise EvidenceError("finalized payout anchor precedes the lifecycle tail")
    if any(integer(row.get("block_number"), "event block") > finalized_block
           for row in events):
        raise EvidenceError("a lifecycle event is after the finalized payout anchor")


def _lifecycle_event(row: Mapping[str, object]) -> dict[str, object]:
    tokens = row.get("token_ids")
    if not isinstance(tokens, list):
        raise EvidenceError("lifecycle event lacks token_ids")
    return {
        "type": str(row["op"]), "block_number": integer(row["source_block_number"], "block"),
        "timestamp": integer(row["source_block_timestamp"], "timestamp"),
        "log_index": integer(row["source_log_index"], "log_index"),
        "tx_hash": str(row["tx_hash"]).lower(), "condition_id": str(row["condition_id"]).lower(),
        "adapter": str(row["adapter"]).lower(), "amount_base": str(row["amount"]),
        "token_ids": [str(token) for token in tokens],
    }


def _window_accounting(
    window, lifecycle: list[dict[str, object]], fills: list[dict[str, object]],
    rebate_base: int | None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    splits = [row for row in lifecycle if row["type"] == "split"]
    merges = [row for row in lifecycle if row["type"] == "merge"]
    if len(splits) != 1 or len(merges) > 1:
        raise EvidenceError("each mapped window needs one split and at most one merge")
    split = splits[0]
    split_amount = integer(split["amount_base"], "split amount")
    tokens = split["token_ids"]
    if not isinstance(tokens, list) or set(tokens) != {window.up_token, window.down_token}:
        raise EvidenceError("split token mapping mismatch")
    if integer(split["timestamp"], "split timestamp") >= window.start:
        raise EvidenceError("split did not precede the market window")
    sales: list[Sale] = []
    for fill in fills:
        if not window.start <= integer(fill["timestamp"], "fill timestamp") < window.start + 300:
            raise EvidenceError("owner fill lies outside its five-minute window")
        sales.append(Sale(
            integer(fill["block_number"], "fill block"), integer(fill["log_index"], "fill log"),
            fill["side"] == "up", integer(fill["shares_base"], "fill shares"),
            integer(fill["cash_delta_base"], "fill cash"),
        ))
    sold = {side: sum(sale.shares_base for sale in sales if sale.side_up == side)
            for side in (True, False)}
    if max(sold.values()) > split_amount:
        raise EvidenceError("observed sales exceed split inventory")
    if merges:
        merge = merges[0]
        expected = min(split_amount - sold[True], split_amount - sold[False])
        if integer(merge["amount_base"], "merge amount") != expected:
            raise EvidenceError("terminal merge does not equal symmetric observed remainder")
        last_fill = max(
            ((sale.block_number, sale.log_index) for sale in sales),
            default=(integer(split["block_number"], "split block"),
                     integer(split["log_index"], "split log")),
        )
        if (integer(merge["block_number"], "merge block"),
                integer(merge["log_index"], "merge log")) <= last_fill:
            raise EvidenceError("merge is not after the last owner fill")
    ordered = sorted([*lifecycle, *fills],
                     key=lambda row: (integer(row["block_number"], "block"),
                                      integer(row["log_index"], "log")))
    inventory, collateral_equivalent, audit_events = {True: 0, False: 0}, 0, []
    for event in ordered:
        kind = event["type"]
        if kind in ("split", "merge"):
            amount = integer(event["amount_base"], str(kind))
            collateral_delta = -amount if kind == "split" else amount
            token_delta = {True: -collateral_delta, False: -collateral_delta}
        else:
            side = event["side"] == "up"
            collateral_delta = integer(event["cash_delta_base"], "fill cash")
            token_delta = {True: 0, False: 0}
            token_delta[side] = -integer(event["shares_base"], "fill shares")
        collateral_equivalent += collateral_delta
        for side in (True, False):
            inventory[side] += token_delta[side]
            if inventory[side] < 0:
                raise EvidenceError("observed token inventory became negative")
        audit_events.append({
            **event,
            "window_ledger_collateral_equivalent_after_base": str(collateral_equivalent),
            "window_observed_up_after_base": str(inventory[True]),
            "window_observed_down_after_base": str(inventory[False]),
        })
    terminal = collateral_equivalent + inventory[bool(window.winner_up)]
    mergeable_pair = min(inventory.values())
    unmatched = {side: inventory[side] - mergeable_pair for side in (True, False)}
    residual_zero_floor = collateral_equivalent + mergeable_pair
    allocations = allocation_diagnostics(sales, bool(window.winner_up))
    fifo_total = allocations["fifo"]["total_pnl_base"]  # type: ignore[index]
    if Fraction(int(fifo_total["numerator"]), int(fifo_total["denominator"])) != terminal:
        raise EvidenceError("event ledger and allocation terminal values differ")
    summary = {
        "slug": window.slug, "start": window.start, "condition_id": window.condition_id.lower(),
        "winner": "up" if window.winner_up else "down", "split_base": str(split_amount),
        "merge_base": str(integer(merges[0]["amount_base"], "merge") if merges else 0),
        "maker_sell_count": len(fills), "sold_up_base": str(sold[True]),
        "sold_down_base": str(sold[False]), "sale_cash_base": str(sum(s.cash_base for s in sales)),
        "ending_up_base": str(inventory[True]), "ending_down_base": str(inventory[False]),
        "split_timestamp": integer(split["timestamp"], "split timestamp"),
        "last_maker_sell_timestamp": (
            None if not fills else max(integer(row["timestamp"], "fill timestamp") for row in fills)
        ),
        "merge_timestamp": (
            None if not merges else integer(merges[0]["timestamp"], "merge timestamp")
        ),
        "market_close_timestamp": window.start + 300,
        "ledger_collateral_equivalent_base": str(collateral_equivalent),
        "contractual_mergeable_pair_base": str(mergeable_pair),
        "unmatched_up_base": str(unmatched[True]),
        "unmatched_down_base": str(unmatched[False]),
        "unmatched_residual_zero_value_base": "0",
        "contractual_pair_recovery_residual_zero_floor_pnl_base": str(
            residual_zero_floor
        ),
        "contractual_terminal_pnl_base": str(terminal),
        "rebate_endpoint_base": None if rebate_base is None else str(rebate_base),
        "diagnostic_terminal_plus_endpoint_rebate_base": (
            None if rebate_base is None else str(terminal + rebate_base)
        ),
        "allocations": allocations,
    }
    return summary, audit_events


def _fraction(value: object) -> Fraction:
    if not isinstance(value, Mapping):
        raise EvidenceError("allocation result is not an exact rational")
    return Fraction(int(str(value.get("numerator"))), int(str(value.get("denominator"))))


def _allocation_totals(windows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    paths = {
        "fifo_paired_pnl_base": ("fifo", "paired_pnl_base"),
        "fifo_residual_pnl_base": ("fifo", "residual_pnl_base"),
        "proportional_paired_pnl_base": ("proportional", "paired_pnl_base"),
        "proportional_residual_pnl_base": ("proportional", "residual_pnl_base"),
        "residual_lower_base": ("bounds", "residual_pnl_lower_base"),
        "residual_upper_base": ("bounds", "residual_pnl_upper_base"),
    }
    totals: dict[str, object] = {}
    for label, (method, field) in paths.items():
        value = Fraction()
        for window in windows:
            allocations = window["allocations"]
            if not isinstance(allocations, Mapping) or not isinstance(allocations[method], Mapping):
                raise EvidenceError("window allocation shape changed")
            value += _fraction(allocations[method][field])
        totals[label] = rational(value)
    return totals


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "attribution", "receipt-cache", "outcomes"):
        parser.add_argument(f"--{name}", type=Path, required=True)
        parser.add_argument(f"--{name}-sha256", required=True)
    parser.add_argument("--rebate", type=Path)
    parser.add_argument("--rebate-sha256")
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--basis", choices=("terminal_accrual", "cash_realized"),
                        default="terminal_accrual")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    wallet = args.wallet.lower()
    if not WALLET_RE.fullmatch(wallet):
        raise SystemExit("--wallet must be a lowercase 20-byte address")
    if args.basis == "cash_realized":
        raise SystemExit("cash_realized unavailable: pUSD and exhaustive ERC-1155 paths are absent")
    if (args.rebate is None) != (args.rebate_sha256 is None):
        raise SystemExit("--rebate and --rebate-sha256 must be supplied together")
    hashes = {
        "candidate": digest(args.candidate_sha256, "candidate SHA-256"),
        "attribution": digest(args.attribution_sha256, "attribution SHA-256"),
        "receipt_cache": digest(args.receipt_cache_sha256, "receipt cache SHA-256"),
        "outcomes": digest(args.outcomes_sha256, "outcomes SHA-256"),
    }
    if args.rebate_sha256 is not None:
        hashes["rebate"] = digest(args.rebate_sha256, "rebate SHA-256")
    repo = Path(__file__).resolve().parents[1]
    start_revision = revision(repo)
    candidate = load_json(args.candidate, hashes["candidate"],
                          "project-fail-adapter-receipt-candidates-v1")
    attribution = load_json(args.attribution, hashes["attribution"],
                            "project-fail-adapter-receipt-attribution-v1")
    outcomes = load_json(args.outcomes, hashes["outcomes"],
                         "project-fail-ctf-payout-evidence-v1")
    rebate = (None if args.rebate is None else
              load_json(args.rebate, hashes["rebate"],
                        "project-fail-maker-rebate-evidence-v1"))
    if attribution.get("input_sha256") != hashes["candidate"]:
        raise EvidenceError("attribution does not bind the candidate artifact")
    windows, query = mapping(candidate)
    conditions = {window.condition_id.lower() for window in windows}
    mapping_sha = str(query["market_mapping_sha256"])
    onchain_winners = verified_winners(
        outcomes, hashes["candidate"], mapping_sha, windows, start_revision
    )
    windows = [
        replace(window, winner_up=int(onchain_winners[window.condition_id.lower()]))
        for window in windows
    ]
    lifecycle_rows, receipt_hashes = attributed_events(
        candidate, attribution, wallet, hashes["candidate"], start_revision
    )
    verify_receipts(args.receipt_cache, hashes["receipt_cache"], receipt_hashes)
    rebate_rows = (None if rebate is None else
                   rebates(rebate, hashes["candidate"], mapping_sha, wallet, conditions))
    t0 = integer(query["lifecycle_start"], "lifecycle_start")
    t1 = integer(query["lifecycle_end_exclusive"], "lifecycle_end_exclusive")
    if t1 - (max(window.start for window in windows) + 300) < MIN_POST_CLOSE_TAIL_S:
        raise EvidenceError("candidate lifecycle ends before the required 24-hour post-close tail")
    client = clickhouse_connect.get_client(host="localhost", port=8123, username="copypoly",
                                           password="copypoly", database="copypoly")
    try:
        raw_fills, raw_redemptions, coverage = source_rows(client, windows, wallet, t0, t1)
    finally:
        client.close()
    fills = fill_events(raw_fills, windows, wallet)
    redemptions = redemption_events(raw_redemptions, conditions, wallet)
    coverage["target_trade_involvement_rows"] = len(raw_fills)
    coverage["accepted_fee_zero_v2_maker_sale_rows"] = len(fills)
    coverage["target_redemption_rows"] = len(redemptions)
    coverage["target_pusd_redemption_rows"] = sum(
        row["collateral_token"] == PUSD
        for row in redemptions
    )
    coverage["raw_fill_condition_rows"] = sum(
        bool(row["raw_condition_id"]) for row in fills
    )
    lifecycle = [_lifecycle_event(row) for row in lifecycle_rows]
    outcome_chain = outcomes.get("chain")
    if not isinstance(outcome_chain, Mapping):
        raise EvidenceError("payout evidence chain anchor is missing")
    _finality_gate(outcome_chain, t1, [*lifecycle, *fills, *redemptions])
    windows_out, accounting_events = [], []
    for window in windows:
        condition = window.condition_id.lower()
        summary, audited = _window_accounting(
            window, [row for row in lifecycle if row["condition_id"] == condition],
            [row for row in fills if row["condition_id"] == condition],
            None if rebate_rows is None else rebate_rows[condition],
        )
        windows_out.append(summary)
        accounting_events.extend(audited)
    rebate_total = None if rebate_rows is None else sum(rebate_rows.values())
    capital = capital_diagnostics(windows_out, accounting_events, t0, t1, rebate_total)
    events = list(accounting_events)
    events.extend(redemptions)
    events.sort(key=lambda row: (integer(row["block_number"], "block"),
                                 integer(row["log_index"], "log"), str(row["type"])))
    terminal = sum(signed_integer(row["contractual_terminal_pnl_base"], "terminal")
                   for row in windows_out)
    zero_floor = sum(signed_integer(
                         row["contractual_pair_recovery_residual_zero_floor_pnl_base"],
                         "floor")
                     for row in windows_out)
    paths = {"candidate": args.candidate, "attribution": args.attribution,
             "receipt_cache": args.receipt_cache, "outcomes": args.outcomes}
    if args.rebate is not None:
        paths["rebate"] = args.rebate
    if (any(hash_file(path) != hashes[name] for name, path in paths.items())
            or start_revision != revision(repo)):
        raise EvidenceError("an input or source revision changed during production")
    sql = coverage.pop("sql")
    payload = {
        "schema": SCHEMA, "basis": "observed_ledger_with_contractual_terminal_mark",
        "scope": {"wallet": wallet, "chain_id": 137, "lifecycle_start": t0,
                   "lifecycle_end_exclusive": t1, "complete_wallet": False,
                   "cash_realized": False, "collateral_cash_path_observed": False,
                   "collateral_equivalent_basis": (
                       "mechanics-implied split/merge principal plus observed sale consideration"
                   )},
        "inputs": {name: {"path": str(paths[name]), "sha256": value}
                   for name, value in hashes.items()},
        "query": {"sql": sql, "sha256": sha256(canonical(sql))},
        "source_coverage": coverage, "revision": start_revision,
        "outcome_evidence": {
            "chain": outcomes["chain"], "rows": len(outcomes["rows"]),
            "payoff_source": "Polygon CTF ConditionResolution and payout state",
            "gamma_crosscheck": "exact_match",
        },
        "market_mapping": [window.__dict__ for window in windows],
        "counts": {"markets": len(windows_out), "maker_sells": len(fills),
                   "splits": sum(row["type"] == "split" for row in lifecycle),
                   "merges": sum(row["type"] == "merge" for row in lifecycle),
                   "redemption_observations": len(redemptions)},
        "events": events, "windows": windows_out, "capital": capital,
        "totals": {"contractual_terminal_pnl_base": str(terminal),
                   "contractual_pair_recovery_residual_zero_floor_pnl_base": str(zero_floor),
                   "split_principal_base": str(sum(integer(row["split_base"], "split")
                                                   for row in windows_out)),
                   "sale_cash_base": str(sum(integer(row["sale_cash_base"], "sale cash")
                                             for row in windows_out)),
                   "merge_return_base": str(sum(integer(row["merge_base"], "merge")
                                                for row in windows_out)),
                   "allocations": _allocation_totals(windows_out),
                   "rebate_overlay": {
                       "endpoint_base": (
                           None if rebate_total is None else str(rebate_total)),
                       "diagnostic_terminal_plus_endpoint_base": (
                           None if rebate_total is None else str(terminal + rebate_total)),
                       "diagnostic_floor_plus_endpoint_base": (
                           None if rebate_total is None else str(zero_floor + rebate_total)),
                       "availability_observed_at": None,
                       "payment_finality": False,
                       "recycled_inside_scope_base": "0",
                   }},
        "limitations": [
            "observed ledger is not a complete wallet balance",
            "split and merge principal are mechanics-implied collateral equivalents, not observed pUSD transfers",
            "contractual terminal marks are not observed redemption cash",
            "erc1155_transfers has no mapped-token coverage and cannot exclude external transfers",
            "usdc_transfers is not integrated and does not establish pUSD cash timing",
            "rebate endpoint amounts have no credit block or payment-finality proof",
            "no immutable historical book supports executable flattening",
            "raw trade_history condition metadata is empty; exact token mapping supplies condition joins",
            "complete pairs are contractually mergeable; only unmatched residuals receive zero in the floor",
            *( [] if rebate_total is not None else
               ["rebate evidence was not supplied; no rebate amount is imputed"] ),
        ],
    }
    raw = canonical(payload) + b"\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as handle:
        handle.write(raw)
    print(f"wrote {args.output} sha256={sha256(raw)} markets={len(windows_out)} "
          f"fills={len(fills)} terminal_base={terminal} rebate_base={rebate_total}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AccountingError, EvidenceError, OSError) as exc:
        raise SystemExit(f"mint accounting refused: {exc}") from exc
