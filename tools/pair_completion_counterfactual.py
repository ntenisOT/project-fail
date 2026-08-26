#!/usr/bin/env python3
"""Run one frozen immediate-completion A/B on a causal paper capture."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import pathlib
import sys
from collections import defaultdict
from collections.abc import Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from paper.cohort_engine import FillRecord, InvalidWindowRecord, SettlementRecord
from paper.pair_types import PairConfig
from paper.replay import ReplayResult, replay_dataset
from paper.replay_data import load_paper_dataset
from paper.strategy_board import (
    canonical_board,
    current_strategy_board,
    execution_model_identity,
    strategy_board_hash,
)


SCHEMA = "project-fail-pair-completion-counterfactual-v1"
CANDIDATE = "basket99_complete99_t0"


def completion_counterfactual_board(action_latency_s: float) -> tuple[PairConfig, ...]:
    """Clone the frozen baseline, changing only identity and taker timing."""
    active = current_strategy_board(action_latency_s)
    # The board now also carries winner-matched mint arms; this counterfactual
    # is defined against the basket99 baseline, which must still be present and
    # unchanged. Isolate it rather than requiring a single-arm board.
    matches = [config for config in active if config.name == "basket99"]
    if len(matches) != 1:
        raise RuntimeError("completion counterfactual requires exactly one basket99 arm")
    baseline = matches[0]
    if baseline.buy_sum_ceiling != 0.99 or not baseline.basket_average_cap:
        raise RuntimeError("basket99 no longer carries the frozen 0.99 basket cap")
    candidate = dataclasses.replace(
        baseline, name=CANDIDATE, buy_taker_after_s=0.0,
    )
    return baseline, candidate


def _metric(row: SettlementRecord, key: str) -> float:
    value = row.metrics.get(key)
    if not isinstance(value, (int, float)):
        raise TypeError(f"settlement metric {key!r} is not numeric")
    return float(value)


def _summary(result: ReplayResult, strategy: str) -> dict[str, object]:
    fills = [
        row for row in result.records
        if isinstance(row, FillRecord) and row.strategy == strategy
    ]
    settled = [
        row for row in result.records
        if isinstance(row, SettlementRecord) and row.strategy == strategy
    ]
    invalid = [
        row for row in result.records
        if isinstance(row, InvalidWindowRecord) and row.strategy == strategy
    ]
    terminal_slugs = {row.slug for row in settled}
    terminal_slugs.update(row.slug for row in invalid)
    finalized_fills = [row for row in fills if row.slug in terminal_slugs]
    unterminated_fills = [row for row in fills if row.slug not in terminal_slugs]
    taker = [row for row in fills if row.action == "taker_buy"]
    buys = [row for row in fills if row.action in ("buy", "taker_buy")]
    final_taker = [row for row in finalized_fills if row.action == "taker_buy"]
    open_taker = [row for row in unterminated_fills if row.action == "taker_buy"]
    final_buys = [
        row for row in finalized_fills if row.action in ("buy", "taker_buy")
    ]
    open_buys = [
        row for row in unterminated_fills if row.action in ("buy", "taker_buy")
    ]
    unsupported = [
        row.action for row in fills
        if row.action not in ("buy", "taker_buy")
    ]
    if unsupported:
        raise ValueError(f"completion counterfactual has unsupported fills: {unsupported}")
    open_by_slug: dict[str, list[FillRecord]] = defaultdict(list)
    for row in open_buys:
        open_by_slug[row.slug].append(row)
    open_up = sum(row.size for row in open_buys if row.outcome_up)
    open_down = sum(row.size for row in open_buys if not row.outcome_up)
    open_paired = sum(min(
        sum(row.size for row in rows if row.outcome_up),
        sum(row.size for row in rows if not row.outcome_up),
    ) for rows in open_by_slug.values())
    open_unmatched = sum(abs(
        sum(row.size for row in rows if row.outcome_up)
        - sum(row.size for row in rows if not row.outcome_up)
    ) for rows in open_by_slug.values())
    open_cash = sum(row.signed_cash for row in unterminated_fills)
    open_floor = sum(
        sum(row.signed_cash for row in rows) + min(
            sum(row.size for row in rows if row.outcome_up),
            sum(row.size for row in rows if not row.outcome_up),
        )
        for rows in open_by_slug.values()
    )
    return {
        "fills": len(fills),
        "finalized_fills": len(finalized_fills),
        "unterminated_fills": len(unterminated_fills),
        "maker_buy_shares": sum(row.size for row in fills if row.action == "buy"),
        "taker_buy_shares": sum(row.size for row in taker),
        "finalized_taker_buy_shares": sum(row.size for row in final_taker),
        "unterminated_taker_buy_shares": sum(row.size for row in open_taker),
        "filled_acquisition_cost_usd": -sum(row.signed_cash for row in buys),
        "finalized_acquisition_cost_usd": -sum(
            row.signed_cash for row in final_buys
        ),
        "unterminated_acquisition_cost_usd": -open_cash,
        "taker_fees_usd": sum(
            max(0.0, -row.signed_cash - row.price * row.size) for row in taker
        ),
        "finalized_taker_fees_usd": sum(
            max(0.0, -row.signed_cash - row.price * row.size)
            for row in final_taker
        ),
        "unterminated_taker_fees_usd": sum(
            max(0.0, -row.signed_cash - row.price * row.size)
            for row in open_taker
        ),
        "settled_windows": len(settled),
        "settled_pnl_usd": sum(row.pnl for row in settled),
        "settled_neutral_usd": sum(
            row.cash + row.resid_shares / 2 for row in settled
        ),
        "settled_adverse_floor_usd": sum(
            row.cash + min(row.residual, row.resid_shares - row.residual)
            for row in settled
        ),
        "settled_unmatched_shares": sum(
            _metric(row, "unmatched_end") for row in settled
        ),
        "settled_buy_pair_shares": sum(
            _metric(row, "buy_pair_shares") for row in settled
        ),
        "invalid_windows": len(invalid),
        "invalid_unmatched_shares": sum(
            abs(row.up_shares - row.down_shares) for row in invalid
        ),
        "invalid_adverse_floor_usd": sum(
            row.cash + min(row.up_shares, row.down_shares) for row in invalid
        ),
        "unterminated_windows_with_fills": len({row.slug for row in unterminated_fills}),
        "unterminated_up_shares": open_up,
        "unterminated_down_shares": open_down,
        "unterminated_paired_shares": open_paired,
        "unterminated_unmatched_shares": open_unmatched,
        "unterminated_adverse_floor_usd": open_floor,
    }


def _summary_number(summary: dict[str, object], key: str) -> float:
    value = summary[key]
    if not isinstance(value, (int, float)):
        raise TypeError(f"strategy summary {key!r} is not numeric")
    return float(value)


def run_counterfactual(
    dataset_path: str | pathlib.Path, *, allow_model_drift: bool = False,
) -> tuple[ReplayResult, dict[str, object]]:
    dataset = load_paper_dataset(dataset_path)
    action_latency = float(str(dataset.runtime.get("action_latency_s")))
    if not math.isfinite(action_latency) or action_latency < 0:
        raise ValueError("captured action latency is invalid")
    board = completion_counterfactual_board(action_latency)
    current_model = execution_model_identity()
    model_match = current_model == dataset.model_identity
    if not model_match and not allow_model_drift:
        raise ValueError(
            "captured execution model differs; rerun only with explicit model-drift consent"
        )
    replay = replay_dataset(
        dataset.path, board, require_board_match=False,
        require_model_match=not allow_model_drift,
    )
    summaries = {config.name: _summary(replay, config.name) for config in board}
    baseline = summaries[board[0].name]
    candidate = summaries[board[1].name]
    numeric_deltas = {
        key: _summary_number(candidate, key) - _summary_number(baseline, key)
        for key in (
            "settled_pnl_usd", "settled_neutral_usd",
            "settled_adverse_floor_usd", "settled_unmatched_shares",
            "invalid_unmatched_shares", "invalid_adverse_floor_usd",
            "filled_acquisition_cost_usd",
            "taker_fees_usd", "finalized_taker_buy_shares",
            "settled_buy_pair_shares", "finalized_taker_fees_usd",
            "unterminated_taker_fees_usd",
            "unterminated_taker_buy_shares", "unterminated_unmatched_shares",
            "unterminated_adverse_floor_usd",
        )
    }
    generator = pathlib.Path(__file__).resolve()
    report: dict[str, object] = {
        "schema": SCHEMA,
        "generator": {
            "path": generator.relative_to(generator.parents[1]).as_posix(),
            "sha256": _sha256(generator),
        },
        "hypothesis": (
            "After the first maker fill, buy the opposite token at the first "
            "captured action-latency-valid tick only when displayed-depth sweep "
            "cost plus taker fees preserves the cumulative 0.99 basket cap."
        ),
        "claim_level": "historical_counterfactual_screen_only",
        "capture": {
            "label": dataset.label,
            "dataset_sha256": dataset.sha256,
            "captured_board_hash": dataset.board_hash,
            "captured_model": dict(dataset.model_identity),
        },
        "counterfactual": {
            "board": json.loads(canonical_board(board)),
            "board_sha256": strategy_board_hash(board),
            "replay_model": current_model,
            "captured_model_matches_replay": model_match,
            "model_drift_explicitly_allowed": allow_model_drift,
            "candidate_changes": {"name": CANDIDATE, "buy_taker_after_s": 0.0},
        },
        "replay_counts": {
            field.name: getattr(replay, field.name)
            for field in dataclasses.fields(ReplayResult)
            if field.name not in {"records", "capture_label", "capture_dataset_sha256"}
        },
        "strategies": summaries,
        "candidate_minus_baseline": numeric_deltas,
        "caveats": [
            "The candidate was evaluated after this tape was observable; this is not prospective validation.",
            "Displayed public taker depth is treated as fully executable at the decision tick.",
            "Authenticated order POST latency and the unresolved 50-vs-250 ms venue taker hold are not modeled.",
            "Invalid-window inventory is reported separately and is excluded from settled PnL.",
            "Unterminated inventory (open-at-stop or finished-unresolved) and its adverse floor are a snapshot, not settlement or validated PnL.",
            "A model-identity mismatch makes this a paired current-engine counterfactual, not live/replay parity.",
        ],
    }
    return replay, report


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset")
    parser.add_argument("--output-dir", required=True, type=pathlib.Path)
    parser.add_argument("--allow-model-drift", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    replay_path = args.output_dir / "counterfactual-replay-ledger.json"
    report_path = args.output_dir / "counterfactual-report.json"
    if replay_path.exists() or report_path.exists():
        raise FileExistsError("refusing to overwrite counterfactual artifacts")
    replay, report = run_counterfactual(
        args.dataset, allow_model_drift=args.allow_model_drift,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    replay_path.write_text(
        json.dumps(dataclasses.asdict(replay), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report["replay_artifact"] = {
        "file": replay_path.name,
        "sha256": _sha256(replay_path),
    }
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(
        f"wrote {replay_path} and {report_path} | "
        f"records={len(replay.records)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
