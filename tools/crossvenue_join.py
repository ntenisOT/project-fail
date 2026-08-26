#!/usr/bin/env python3
"""Fail-closed offline binding of paper and cross-venue capture datasets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
from collections.abc import Mapping, Sequence

import paper.replay_data as replay_data
import tools.crossvenue_dataset as crossvenue_dataset
from paper.replay_data import CaptureIntegrityError, load_paper_dataset
from tools.crossvenue_dataset import (
    JoinIntegrityError,
    count_value,
    file_sha256,
    load_cross_dataset,
    verified_raw_manifest,
)
from tools.transport_telemetry import validated_revision


SCHEMA = "project-fail-crossvenue-paper-join-v1"
GAMMA_REGIME_SCHEMA = "project-fail-gamma-resolution-regimes-v1"
REQUIRED_ARTIFACTS = frozenset({"cohort", "wallet_fills", "markets", "gamma"})
MAX_CLOCK_OFFSET_DELTA_NS = 50_000_000
WINDOW_NS = 300_000_000_000
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def _market_slugs(path: pathlib.Path) -> set[str]:
    slugs: set[str] = set()
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                slug = str(row.get("slug") or "") if isinstance(row, dict) else ""
                if not slug or slug in slugs:
                    raise JoinIntegrityError(
                        f"invalid or duplicate market slug at line {line_number}"
                    )
                slugs.add(slug)
    except (OSError, json.JSONDecodeError) as exc:
        raise JoinIntegrityError(f"invalid market artifact: {exc}") from exc
    if not slugs:
        raise JoinIntegrityError("market artifact has no rows")
    return slugs


def _gamma_regimes(path: pathlib.Path) -> tuple[set[str], dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JoinIntegrityError(f"invalid Gamma resolution regimes: {exc}") from exc
    if not isinstance(value, dict):
        raise JoinIntegrityError("Gamma resolution-regime artifact is not an object")
    raw_rows = value.get("rows")
    if value.get("schema") != GAMMA_REGIME_SCHEMA or not isinstance(raw_rows, list):
        raise JoinIntegrityError("unsupported Gamma resolution-regime schema")
    slugs: set[str] = set()
    lookbacks: set[int] = set()
    sources: set[str] = set()
    for index, item in enumerate(raw_rows):
        if not isinstance(item, dict):
            raise JoinIntegrityError(f"invalid Gamma regime row {index}")
        slug = str(item.get("slug") or "")
        source = str(item.get("resolution_source") or "")
        config_id = str(item.get("config_id") or "")
        lookback = count_value(item.get("lookback_s"), "Gamma lookback_s")
        opening = str(item.get("price_to_beat") or "")
        final = str(item.get("final_price") or "")
        if (not slug or slug in slugs or not source or not config_id
                or lookback == 0 or not opening or not final):
            raise JoinIntegrityError(f"incomplete Gamma regime row {index}")
        slugs.add(slug)
        sources.add(source)
        lookbacks.add(lookback)
    if not slugs:
        raise JoinIntegrityError("Gamma resolution-regime artifact has no rows")
    return slugs, {
        "markets": len(slugs), "lookback_s": sorted(lookbacks),
        "resolution_sources": sorted(sources),
        "policy": "per_market_from_gamma_never_global_default",
    }


def _artifact_rows(artifacts: Mapping[str, pathlib.Path]) -> dict[str, object]:
    if not REQUIRED_ARTIFACTS <= set(artifacts):
        missing = sorted(REQUIRED_ARTIFACTS - set(artifacts))
        raise JoinIntegrityError(f"required passive artifacts are missing: {missing}")
    rows: dict[str, object] = {}
    for name, path in sorted(artifacts.items()):
        if not NAME_RE.fullmatch(name):
            raise JoinIntegrityError(f"invalid artifact name: {name}")
        try:
            size, digest = path.stat().st_size, file_sha256(path)
        except OSError as exc:
            raise JoinIntegrityError(f"artifact is missing: {path}") from exc
        if size == 0:
            raise JoinIntegrityError(f"artifact is empty: {path}")
        rows[name] = {"path": path.as_posix(), "sha256": digest, "bytes": size}
    gamma_slugs, gamma_summary = _gamma_regimes(artifacts["gamma"])
    if _market_slugs(artifacts["markets"]) != gamma_slugs:
        raise JoinIntegrityError("Gamma resolution regimes do not cover exact markets")
    gamma_row = rows["gamma"]
    assert isinstance(gamma_row, dict)
    gamma_row["regimes"] = gamma_summary
    return rows


def _anchor(row: Mapping[str, object], kind: str) -> tuple[int, int]:
    return (
        count_value(row.get("wall_ns"), f"{kind} wall_ns"),
        count_value(row.get("monotonic_ns"), f"{kind} monotonic_ns"),
    )


def _analysis_identity(revision: str) -> dict[str, object]:
    sources = tuple(pathlib.Path(path) for path in (
        __file__, crossvenue_dataset.__file__, replay_data.__file__,
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
    if (paper.clock_domain is None) != (cross.clock_domain is None):
        raise JoinIntegrityError("only one capture has explicit clock identity")
    if paper.clock_domain is not None and paper.clock_domain != cross.clock_domain:
        raise JoinIntegrityError("paper and cross-venue clock domains differ")
    clock_mode = "explicit_host_boot" if paper.clock_domain is not None else "inferred_offset"

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
        },
        "crossvenue": {
            "path": cross.path.as_posix(), "sha256": cross.sha256,
            "label": cross.label, "asset": cross.asset, "revision": cross.revision,
            "sources": dict(cross.sources), "disconnect_gaps": list(cross.gaps),
        },
        "clock_evidence": {
            "mode": clock_mode,
            "identity": None if paper.clock_domain is None else dict(paper.clock_domain),
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
        "passive_artifacts": _artifact_rows(artifacts),
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
