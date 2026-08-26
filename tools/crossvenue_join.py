#!/usr/bin/env python3
"""Fail-closed offline binding of paper and cross-venue capture datasets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
from collections.abc import Mapping, Sequence

import paper.replay_data as replay_data
import tools.crossvenue_dataset as crossvenue_dataset
import tools.crossvenue_gaps as crossvenue_gaps
import tools.winner_artifacts as winner_artifacts
from paper.replay_data import CaptureIntegrityError, load_paper_dataset
from tools.crossvenue_dataset import (
    JoinIntegrityError,
    count_value,
    load_cross_dataset,
    verified_raw_manifest,
)
from tools.transport_telemetry import validated_revision
from tools.winner_artifacts import validate_artifacts


SCHEMA = "project-fail-crossvenue-paper-join-v1"
MAX_CLOCK_OFFSET_DELTA_NS = 50_000_000
WINDOW_NS = 300_000_000_000


def _anchor(row: Mapping[str, object], kind: str) -> tuple[int, int]:
    return (
        count_value(row.get("wall_ns"), f"{kind} wall_ns"),
        count_value(row.get("monotonic_ns"), f"{kind} monotonic_ns"),
    )


def _analysis_identity(revision: str) -> dict[str, object]:
    sources = tuple(pathlib.Path(path) for path in (
        __file__, crossvenue_dataset.__file__, crossvenue_gaps.__file__,
        winner_artifacts.__file__, replay_data.__file__,
    ))
    digest = hashlib.sha256()
    rows: list[dict[str, object]] = []
    root = pathlib.Path(__file__).resolve().parents[1]
    for path in sources:
        resolved = path.resolve()
        name = resolved.relative_to(root).as_posix()
        payload = resolved.read_bytes()
        digest.update(name.encode() + b"\0" + payload)
        rows.append({"path": name, "sha256": hashlib.sha256(payload).hexdigest()})
    return {"revision": revision, "sha256": digest.hexdigest(), "sources": rows}


def build_join(
    paper_path: pathlib.Path, cross_path: pathlib.Path,
    artifacts: Mapping[str, pathlib.Path], analysis_revision: str,
) -> dict[str, object]:
    try:
        paper = load_paper_dataset(paper_path)
    except CaptureIntegrityError as exc:
        raise JoinIntegrityError(f"invalid paper dataset: {exc}") from exc
    paper_raw = verified_raw_manifest(paper.raw_manifest)
    paper_causal = verified_raw_manifest(paper.causal_manifest)
    cross = load_cross_dataset(cross_path)
    revision = validated_revision(analysis_revision)
    assets = paper.runtime.get("assets")
    if not isinstance(assets, list) or cross.asset not in assets:
        raise JoinIntegrityError("paper and cross-venue asset scopes do not match")

    anchors = {
        "paper_start": _anchor(paper.events[0], "paper_start"),
        "paper_end": _anchor(paper.events[-1], "paper_end"),
        "crossvenue_start": _anchor(cross.start, "crossvenue_start"),
        "crossvenue_end": _anchor(cross.end, "crossvenue_end"),
    }
    offsets = {name: wall - mono for name, (wall, mono) in anchors.items()}
    offset_delta = max(offsets.values()) - min(offsets.values())
    if offset_delta > MAX_CLOCK_OFFSET_DELTA_NS:
        raise JoinIntegrityError("paper and cross-venue clock anchors do not agree")
    paper_clock, cross_clock = paper.clock_domain, cross.clock_domain
    if paper_clock is None and any(
        row.get("kind") in {"disconnect", "connection_failure"}
        for row in paper.events
    ):
        raise JoinIntegrityError("legacy paper transport gap lacks clock identity")
    if paper_clock is not None and cross_clock is not None:
        if paper_clock != cross_clock:
            raise JoinIntegrityError("paper and cross-venue clock domains differ")
        clock_mode = "explicit_host_boot"
        explicit_identity = paper_clock
        identity_source = "paper_and_crossvenue"
    elif paper_clock is None and cross_clock is None:
        clock_mode = "inferred_offset"
        explicit_identity = None
        identity_source = None
    else:
        clock_mode = "mixed_inferred_to_explicit_host_boot"
        explicit_identity = paper_clock if paper_clock is not None else cross_clock
        identity_source = "paper" if paper_clock is not None else "crossvenue"

    overlap_start = max(anchor[0] for name, anchor in anchors.items() if name.endswith("start"))
    overlap_end = min(anchor[0] for name, anchor in anchors.items() if name.endswith("end"))
    if overlap_end - overlap_start < WINDOW_NS:
        raise JoinIntegrityError("captures have less than one five-minute overlap")
    first_window = (overlap_start + WINDOW_NS - 1) // WINDOW_NS * WINDOW_NS
    last_window = (overlap_end - WINDOW_NS) // WINDOW_NS * WINDOW_NS
    if last_window < first_window:
        raise JoinIntegrityError("captures contain no complete UTC-aligned window")

    return {
        "schema": SCHEMA,
        "claim_limits": {
            "event": "first_observed_fill_block",
            "order_timing_identified": False, "causal_effect_identified": False,
            "subsecond_reaction_identified": False, "strategy_validated": False,
        },
        "analysis_identity": _analysis_identity(revision),
        "feature_join_contract": {
            "pre_close_external_flow": "crossvenue Binance raw source/publisher time",
            "near_money_polymarket_state": "paper processed book/price_change handler time",
            "official_open_and_final": "per-market hashed Gamma resolution regime",
            "post_close_reversal": "crossvenue external frames after market close",
            "gamma_required_fields": [
                "slug", "resolution_source", "lookback_s", "config_id",
                "price_to_beat", "final_price",
            ],
            "hardcoded_twap_regime": False,
        },
        "paper": {
            "path": paper.path.as_posix(), "sha256": paper.sha256,
            "label": paper.label, "board_hash": paper.board_hash,
            "model_identity": dict(paper.model_identity), "runtime": dict(paper.runtime),
            "raw": paper_raw, "causal": paper_causal,
            "transport_gaps": list(paper.transport_gaps),
            "transport_gap_policy": "all_exact_paper_socket_unavailable_intervals",
            "book_chain_gaps": None,
            "book_chain_gap_status": "not_materialized_event_study_no_go",
        },
        "crossvenue": {
            "path": cross.path.as_posix(), "sha256": cross.sha256,
            "label": cross.label, "asset": cross.asset, "revision": cross.revision,
            "sources": dict(cross.sources), "disconnect_gaps": list(cross.gaps),
            "gap_policy": "all_exact_source_unavailable_intervals",
        },
        "clock_evidence": {
            "mode": clock_mode,
            "identity": (None if explicit_identity is None
                         else dict(explicit_identity)),
            "explicit_identity_source": identity_source,
            "wall_minus_monotonic_ns": offsets,
            "max_offset_delta_ns": offset_delta,
            "allowed_offset_delta_ns": MAX_CLOCK_OFFSET_DELTA_NS,
        },
        "overlap": {
            "start_wall_ns": overlap_start, "end_wall_ns": overlap_end,
            "duration_s": (overlap_end - overlap_start) / 1_000_000_000,
            "first_complete_window_start": first_window // 1_000_000_000,
            "last_complete_window_start": last_window // 1_000_000_000,
        },
        "passive_artifacts": validate_artifacts(
            artifacts, overlap_start_s=overlap_start // 1_000_000_000,
            first_window=first_window // 1_000_000_000,
            last_window=last_window // 1_000_000_000,
        ),
    }


def _artifact_args(values: Sequence[str]) -> dict[str, pathlib.Path]:
    result: dict[str, pathlib.Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or name in result:
            raise ValueError(f"artifact must be unique NAME=PATH: {value}")
        result[name] = pathlib.Path(raw_path)
    return result


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper", required=True)
    parser.add_argument("--crossvenue", required=True)
    parser.add_argument("--artifact", action="append", required=True)
    parser.add_argument("--analysis-revision", required=True, type=validated_revision)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        args.artifacts = _artifact_args(args.artifact)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def _write(path: pathlib.Path, payload: Mapping[str, object]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    report = build_join(
        pathlib.Path(args.paper), pathlib.Path(args.crossvenue), args.artifacts,
        args.analysis_revision,
    )
    _write(pathlib.Path(args.output), report)
    print(json.dumps({"output": args.output, "schema": SCHEMA}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
