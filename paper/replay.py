"""Deterministic single-pass replay of an immutable paper-feed capture."""

from __future__ import annotations

import argparse
import dataclasses
import heapq
import json
import math
import pathlib
from collections.abc import Iterable, Iterator, Mapping, Sequence

from paper.capture import (
    CAUSAL_EVENT,
    CAUSAL_EVENT_RECORD,
    CAUSAL_TICK,
    CAUSAL_TICK_RECORD,
)
from paper.cohort_engine import (
    CohortEngine,
    CohortRecord,
    InvalidWindowRecord,
    SettlementRecord,
)
from paper.market_metadata import ActiveMarket
from paper.pair_types import PairConfig
from paper.replay_data import (
    CaptureIntegrityError,
    PaperDataset,
    RawFrame,
    iter_raw_frames,
    load_paper_dataset,
)
from paper.strategy_board import (
    current_strategy_board,
    execution_model_identity,
    strategy_board_hash,
)


@dataclasses.dataclass(frozen=True)
class ReplayResult:
    records: tuple[CohortRecord, ...]
    capture_label: str
    capture_dataset_sha256: str
    frames: int
    market_events: int
    decision_ticks: int
    parse_errors: int
    captured_board_hash: str
    replay_board_hash: str
    captured_model_hash: str
    replay_model_hash: str
    opened_markets: int
    finished_markets: int
    resolved_markets: int
    open_at_end: int
    finished_unresolved: int
    settled_strategy_windows: int
    invalid_strategy_windows: int


@dataclasses.dataclass(frozen=True)
class _Point:
    monotonic_ns: int
    priority: int
    index: int
    wall_ns: int
    kind: str
    value: object


def _causal_points(dataset: PaperDataset) -> Iterator[_Point]:
    for record in iter_raw_frames(dataset.causal_manifest):
        if len(record.payload) == CAUSAL_EVENT_RECORD.size:
            kind, frame_id, event_index = CAUSAL_EVENT_RECORD.unpack(record.payload)
            if kind == CAUSAL_EVENT:
                yield _Point(
                    record.monotonic_ns, 1, record.index, record.wall_ns,
                    "event", (frame_id, event_index),
                )
                continue
        if record.payload == CAUSAL_TICK_RECORD.pack(CAUSAL_TICK):
            yield _Point(
                record.monotonic_ns, 1, record.index, record.wall_ns, "tick", None,
            )
            continue
        raise CaptureIntegrityError(
            f"invalid causal record in {record.chunk} at index {record.index}"
        )


def _marker_points(dataset: PaperDataset) -> Iterator[_Point]:
    for index, marker in enumerate(dataset.events):
        yield _Point(
            int(str(marker["monotonic_ns"])), 0, index,
            int(str(marker["wall_ns"])), "marker", marker,
        )


def _events(frame: RawFrame) -> tuple[dict[str, object], ...] | None:
    try:
        payload = json.loads(frame.payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return (payload,)
    if isinstance(payload, list):
        return tuple(row for row in payload if isinstance(row, dict))
    return ()


class _RawEventResolver:
    """Resolve monotonic processed IDs while fully validating the raw capture."""

    def __init__(self, dataset: PaperDataset) -> None:
        self._frames = iter(iter_raw_frames(dataset.raw_manifest))
        self._current: RawFrame | None = None
        self._decoded: tuple[dict[str, object], ...] | None = ()
        self._last_event_id = (-1, -1)
        self.frames = 0
        self.parse_errors = 0

    def _advance(self) -> bool:
        try:
            frame = next(self._frames)
        except StopIteration:
            self._current = None
            self._decoded = ()
            return False
        self._current = frame
        self._decoded = _events(frame)
        self.frames += 1
        if self._decoded is None:
            self.parse_errors += 1
        return True

    def resolve(self, frame_id: int, event_index: int) -> dict[str, object]:
        event_id = (frame_id, event_index)
        if event_id <= self._last_event_id:
            raise CaptureIntegrityError("processed event identifiers are not increasing")
        self._last_event_id = event_id
        while self._current is None or self._current.index < frame_id:
            if not self._advance():
                raise CaptureIntegrityError(
                    f"processed event references missing raw frame {frame_id}"
                )
        if self._current.index != frame_id:
            raise CaptureIntegrityError(
                f"processed event references skipped raw frame {frame_id}"
            )
        if self._decoded is None:
            raise CaptureIntegrityError(
                f"processed event references invalid JSON frame {frame_id}"
            )
        if event_index >= len(self._decoded):
            raise CaptureIntegrityError(
                f"processed event index {event_index} is absent from frame {frame_id}"
            )
        return self._decoded[event_index]

    def finish(self) -> None:
        while self._advance():
            pass


def _market(marker: Mapping[str, object]) -> ActiveMarket:
    return ActiveMarket(
        str(marker["asset"]), str(marker["slug"]), int(str(marker["start"])),
        str(marker["condition_id"]), str(marker["up_token"]),
        str(marker["down_token"]), float(str(marker["min_order_size"])),
    )


def replay_dataset(
    path: str | pathlib.Path, configs: Sequence[PairConfig] | None = None, *,
    require_board_match: bool = True, require_model_match: bool | None = None,
) -> ReplayResult:
    dataset = load_paper_dataset(path)
    action_latency = float(str(dataset.runtime["action_latency_s"]))
    if not math.isfinite(action_latency) or action_latency < 0:
        raise ValueError("captured action latency is invalid")
    board = (
        tuple(configs) if configs is not None else current_strategy_board(action_latency)
    )
    replay_hash = strategy_board_hash(board)
    if require_board_match and replay_hash != dataset.board_hash:
        raise ValueError("replay strategy board does not match the captured paper board")
    max_lag = float(str(dataset.runtime["max_market_event_lag_s"]))
    if not math.isfinite(max_lag) or max_lag <= 0:
        raise ValueError("captured maximum market-event lag is invalid")
    replay_model = execution_model_identity()
    captured_model_hash = str(dataset.model_identity.get("sha256") or "")
    replay_model_hash = str(replay_model.get("sha256") or "")
    enforce_model = require_board_match if require_model_match is None else require_model_match
    if enforce_model and replay_model != dataset.model_identity:
        raise ValueError("replay execution model does not match the captured paper model")
    engine = CohortEngine(board, max_event_lag_s=max_lag)
    points: Iterable[_Point] = heapq.merge(
        _marker_points(dataset), _causal_points(dataset),
        key=lambda point: (point.monotonic_ns, point.priority, point.index),
    )
    raw = _RawEventResolver(dataset)
    records: list[CohortRecord] = []
    market_events = decision_ticks = 0
    opened: set[str] = set()
    finished: set[str] = set()
    resolved: set[str] = set()

    for point in points:
        if point.kind == "event":
            event_id = point.value
            if (not isinstance(event_id, tuple) or len(event_id) != 2
                    or not all(isinstance(value, int) for value in event_id)):
                raise CaptureIntegrityError("causal event identifier is invalid")
            frame_id, event_index = event_id
            event = raw.resolve(frame_id, event_index)
            market_events += 1
            records.extend(engine.on_event(event, point.wall_ns / 1e9))
            continue
        if point.kind == "tick":
            records.extend(engine.tick(point.wall_ns / 1e9))
            decision_ticks += 1
            continue

        marker = point.value
        assert isinstance(marker, dict)
        kind = str(marker.get("kind") or "")
        if kind == "market_open":
            market = _market(marker)
            engine.open_market(market, float(str(marker["observed_at"])))
            if market.slug in opened:
                raise CaptureIntegrityError(f"duplicate market_open for {market.slug}")
            opened.add(market.slug)
        elif kind == "market_finish":
            asset = str(marker["asset"])
            engine.finish_window(asset, float(str(marker["observed_at"])))
            slug = str(marker["slug"])
            if slug not in opened or slug in finished:
                raise CaptureIntegrityError(f"invalid market_finish for {slug}")
            finished.add(slug)
        elif kind == "disconnect":
            engine.disconnect(float(str(marker.get("observed_at", point.wall_ns / 1e9))))
        elif kind == "resolution":
            slug = str(marker["slug"])
            if slug not in finished or slug in resolved:
                raise CaptureIntegrityError(f"invalid resolution for {slug}")
            records.extend(engine.settle(
                str(marker["asset"]), int(str(marker["winner_up"])),
                float(str(marker["observed_at"])), slug=slug,
            ))
            resolved.add(slug)

    raw.finish()
    return ReplayResult(
        tuple(records), dataset.label, dataset.sha256,
        raw.frames, market_events, decision_ticks, raw.parse_errors,
        dataset.board_hash, replay_hash, captured_model_hash, replay_model_hash,
        len(opened), len(finished), len(resolved), len(opened - finished),
        len(finished - resolved),
        sum(isinstance(row, SettlementRecord) for row in records),
        sum(isinstance(row, InvalidWindowRecord) for row in records),
    )


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset")
    parser.add_argument("--output", required=True)
    parser.add_argument("--counterfactual", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    result = replay_dataset(
        args.dataset, require_board_match=not args.counterfactual,
        require_model_match=not args.counterfactual,
    )
    output = pathlib.Path(args.output)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(dataclasses.asdict(result), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {output} | frames={result.frames} records={len(result.records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
