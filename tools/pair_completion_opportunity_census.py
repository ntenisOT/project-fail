#!/usr/bin/env python3
"""Census first-leg completion opportunities without applying a policy."""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import pathlib
import sys
from collections import Counter
from collections.abc import Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from paper.cohort_engine import CohortEngine
from paper.replay import (
    _RawEventResolver,
    _causal_points,
    _market,
    _marker_points,
)
from paper.replay_data import CaptureIntegrityError, load_paper_dataset
from paper.strategy_board import (
    canonical_board,
    execution_model_identity,
    strategy_board_hash,
)
from tools.pair_completion_counterfactual import completion_counterfactual_board
from tools.pair_completion_opportunity_core import (
    OpportunityCensus,
    _state,
    _window,
)


SCHEMA = "project-fail-pair-completion-opportunity-census-v1"


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _module_source(module_name: str, label: str) -> pathlib.Path:
    module = sys.modules.get(module_name)
    source = getattr(module, "__file__", None)
    if not isinstance(source, str) or not source:
        raise RuntimeError(f"cannot identify {label} source")
    return pathlib.Path(source).resolve()


def run_census(
    dataset_path: str | pathlib.Path, *, allow_model_drift: bool = False,
) -> dict[str, object]:
    generator = pathlib.Path(__file__).resolve()
    core_source = _module_source(OpportunityCensus.__module__, "census-core")
    spec_source = _module_source(
        completion_counterfactual_board.__module__, "completion-spec",
    )
    generator_hash = _sha256(generator)
    core_source_hash = _sha256(core_source)
    spec_source_hash = _sha256(spec_source)
    dataset = load_paper_dataset(dataset_path)
    latency = float(str(dataset.runtime.get("action_latency_s")))
    max_lag = float(str(dataset.runtime.get("max_market_event_lag_s")))
    if (not math.isfinite(latency) or latency < 0
            or not math.isfinite(max_lag) or max_lag <= 0):
        raise ValueError("captured paper timing parameters are invalid")
    baseline, completion = completion_counterfactual_board(latency)
    baseline_hash = strategy_board_hash((baseline,))
    board_match = baseline_hash == dataset.board_hash
    current_model = execution_model_identity()
    model_match = current_model == dataset.model_identity
    if not model_match and not allow_model_drift:
        raise ValueError(
            "captured execution model differs; rerun only with explicit model-drift consent"
        )
    if not board_match and not allow_model_drift:
        raise ValueError(
            "captured strategy board differs; rerun only with explicit model-drift consent"
        )
    engine = CohortEngine((baseline,), max_event_lag_s=max_lag)
    census = OpportunityCensus(completion)
    points = heapq.merge(
        _marker_points(dataset), _causal_points(dataset),
        key=lambda point: (point.monotonic_ns, point.priority, point.index),
    )
    raw = _RawEventResolver(dataset)
    market_events = ticks = 0
    opened: set[str] = set()
    finished: set[str] = set()
    resolved: set[str] = set()
    for point in points:
        known_at = point.wall_ns / 1e9
        if point.kind == "event":
            event_id = point.value
            if (not isinstance(event_id, tuple) or len(event_id) != 2
                    or not all(isinstance(value, int) for value in event_id)):
                raise CaptureIntegrityError("causal event identifier is invalid")
            frame_id, event_index = event_id
            event = raw.resolve(frame_id, event_index)
            market_events += 1
            before = {
                asset: _state(_window(engine, asset))
                for asset in engine._active
            } if event.get("event_type") == "last_trade_price" else {}
            fills = engine.on_event(event, known_at)
            for fill in fills:
                window = _window(engine, fill.asset)
                census.after_fill(
                    window, before[fill.asset], _state(window), fill, known_at,
                )
            for asset in list(census.active):
                window = _window(engine, asset)
                if not window.full_window:
                    census.censor(
                        asset, known_at, known_at,
                        window.invalid_reason or "invalid_window", window,
                    )
            continue
        if point.kind == "tick":
            for asset in list(census.active):
                if asset in engine._stale_assets:
                    continue
                window = _window(engine, asset)
                cohort = engine._active[asset]
                up = engine.books.get(cohort.market.up_token)
                down = engine.books.get(cohort.market.down_token)
                if up is not None and down is not None:
                    census.observe_tick(window, up, down, known_at)
            engine.tick(known_at)
            ticks += 1
            continue

        marker = point.value
        if not isinstance(marker, Mapping):
            raise RuntimeError("capture marker is not a mapping")
        kind = str(marker.get("kind") or "")
        observed = float(str(marker.get("observed_at", known_at)))
        if kind == "market_open":
            market = _market(marker)
            if market.slug in opened:
                raise CaptureIntegrityError(f"duplicate market_open for {market.slug}")
            engine.open_market(market, observed)
            opened.add(market.slug)
        elif kind == "market_finish":
            asset = str(marker["asset"])
            slug = str(marker["slug"])
            if slug not in opened or slug in finished:
                raise CaptureIntegrityError(f"invalid market_finish for {slug}")
            census.censor(asset, observed, observed, "market_finish", _window(engine, asset))
            engine.finish_window(asset, observed)
            finished.add(slug)
        elif kind == "disconnect":
            for asset in list(census.active):
                census.censor(
                    asset, observed, observed, "ws_reconnect", _window(engine, asset),
                )
            engine.disconnect(observed)
        elif kind == "resolution":
            slug = str(marker["slug"])
            if slug not in finished or slug in resolved:
                raise CaptureIntegrityError(f"invalid resolution for {slug}")
            engine.settle(
                str(marker["asset"]), int(str(marker["winner_up"])), observed,
                slug=slug,
            )
            resolved.add(slug)
        elif kind == "run_end":
            for asset in list(census.active):
                census.censor(
                    asset, observed, observed, "capture_stop", _window(engine, asset),
                )
    raw.finish()
    if execution_model_identity() != current_model:
        raise RuntimeError("paper execution model changed during census")
    if (_sha256(generator) != generator_hash
            or _sha256(core_source) != core_source_hash
            or _sha256(spec_source) != spec_source_hash):
        raise RuntimeError("census source changed during census")
    if census.active:
        raise RuntimeError("census ended with uncensored first-leg episodes")
    for row in census.rows:
        slug = str(row["slug"])
        if slug in resolved:
            lifecycle = "resolved"
        elif slug in finished:
            lifecycle = "finished_unresolved"
        elif slug in opened:
            lifecycle = "open_at_stop"
        else:
            raise CaptureIntegrityError(f"episode references unopened market {slug}")
        row["capture_market_lifecycle"] = lifecycle
        opportunity = row["opportunity"]
        if opportunity is None:
            continue
        if not isinstance(opportunity, dict):
            raise RuntimeError("census opportunity is not an object")
        pair_average = float(str(opportunity["cumulative_pair_average_after"]))
        if pair_average > completion.buy_sum_ceiling + 1e-9:
            raise RuntimeError("census emitted an above-cap completion opportunity")
        if float(str(row["known_seconds_to_endpoint"])) + 1e-9 < latency:
            raise RuntimeError("census emitted a pre-latency completion opportunity")
        if opportunity.get("clears_open_pair") is not True:
            raise RuntimeError("census emitted a partial completion opportunity")
    endpoints = Counter(str(row["endpoint"]) for row in census.rows)
    censor_reasons = Counter(
        str(row["censor_reason"])
        for row in census.rows if row["censor_reason"] is not None
    )
    endpoint_lifecycles = {
        lifecycle: dict(sorted(Counter(
            str(row["endpoint"]) for row in census.rows
            if row["capture_market_lifecycle"] == lifecycle
        ).items()))
        for lifecycle in ("resolved", "finished_unresolved", "open_at_stop")
    }
    return {
        "schema": SCHEMA,
        "claim_level": "descriptive_opportunity_census_only",
        "generator": {
            "path": generator.relative_to(generator.parents[1]).as_posix(),
            "sha256": generator_hash,
            "core_source": core_source.relative_to(core_source.parents[1]).as_posix(),
            "core_source_sha256": core_source_hash,
            "spec_source": spec_source.relative_to(spec_source.parents[1]).as_posix(),
            "spec_source_sha256": spec_source_hash,
        },
        "capture": {
            "label": dataset.label,
            "dataset_sha256": dataset.sha256,
            "captured_board_hash": dataset.board_hash,
            "captured_model": dict(dataset.model_identity),
        },
        "observation_model": {
            "baseline_board": json.loads(canonical_board((baseline,))),
            "baseline_board_sha256": baseline_hash,
            "completion_spec_board_sha256": strategy_board_hash((baseline, completion)),
            "captured_action_latency_s": latency,
            "captured_max_market_event_lag_s": max_lag,
            "replay_model": current_model,
            "captured_model_matches_replay": model_match,
            "captured_board_matches_baseline": board_match,
            "model_drift_explicitly_allowed": allow_model_drift,
        },
        "counts": {
            "raw_frames": raw.frames,
            "parse_errors": raw.parse_errors,
            "market_events": market_events,
            "decision_ticks": ticks,
            "opened_markets": len(opened),
            "finished_markets": len(finished),
            "resolved_markets": len(resolved),
            "open_at_end": len(opened - finished),
            "finished_unresolved": len(finished - resolved),
            "episodes": len(census.rows),
            "endpoints": dict(sorted(endpoints.items())),
            "endpoints_by_capture_market_lifecycle": endpoint_lifecycles,
            "censor_reasons": dict(sorted(censor_reasons.items())),
        },
        "field_semantics": {
            "opportunity": (
                "First captured decision tick after modeled action latency where a "
                "full displayed-depth sweep plus per-leg taker fees preserves the "
                "cumulative 0.99 basket average."
            ),
            "reentry_state_enabled": (
                "Hypothetical completion clears FIFO imbalance early enough for one "
                "captured action-latency interval before cutoff and leaves balanced "
                "clip capacity; it does not assert a future quote or fill."
            ),
            "fee_plus_spread_insurance_cost_usd": (
                "Fee-inclusive sweep cost minus the cost of the same shares at the "
                "baseline resting opposite maker bid. It can be negative when the "
                "displayed ask moved below that simulated resting bid; it is not an "
                "observed execution cost."
            ),
        },
        "unidentifiable": [
            "Authenticated POST latency and the venue 50-vs-250 ms taker hold.",
            "Whether displayed depth remains executable after either hold.",
            "Actual re-entry or later PnL after a hypothetical completion changes state.",
            "Policy-dependent first-leg episodes that only a hypothetical re-entry creates.",
            "Insurance cost when no sufficiently sized opposite maker order is resting.",
        ],
        "caveats": [
            "This is a current-engine observation over previously visible tapes, not prospective validation.",
            "A model or board identity mismatch makes it non-parity historical analysis.",
            "Natural completion is queue-model output, not evidence that our live order filled.",
            "No hypothetical completion is applied and no policy PnL is computed.",
        ],
        "episodes": census.rows,
    }


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset")
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--allow-model-drift", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    report = run_census(args.dataset, allow_model_drift=args.allow_model_drift)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
        handle.flush()
    episodes = report.get("episodes")
    if not isinstance(episodes, list):
        raise RuntimeError("census report lacks episode rows")
    print(f"wrote {args.output} | episodes={len(episodes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
