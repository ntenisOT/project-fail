"""Optional immutable capture owned by the paper runner's market connection."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import struct
import threading
import time
from collections.abc import Mapping

from paper.market_metadata import ActiveMarket
from paper.strategy_board import execution_model_identity
from tools.market_windows import ResolvedWindow
from tools.transport_telemetry import (
    CaptureWriteError,
    JsonlSink,
    RawFrameWriter,
    clock_domain_identity,
)


CAUSAL_EVENT = 1
CAUSAL_TICK = 2
CAUSAL_EVENT_RECORD = struct.Struct("!BQI")
CAUSAL_TICK_RECORD = struct.Struct("!B")


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class PaperCapture:
    def __init__(
        self, directory: pathlib.Path, label: str, *, board_hash: str,
        runtime: Mapping[str, object], limit_bytes: int, chunk_bytes: int,
        model_identity: Mapping[str, object] | None = None,
    ) -> None:
        if not label or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                            for char in label):
            raise ValueError("capture label must contain only letters, digits, dash, underscore")
        self.directory = directory
        self.label = label
        self.board_hash = board_hash
        self.runtime = dict(runtime)
        self.model_identity = dict(model_identity or execution_model_identity())
        self.clock_domain = clock_domain_identity()
        self.started_at = time.time()
        self.events_path = directory / f"{label}.events.jsonl"
        self.dataset_path = directory / f"{label}.dataset.json"
        causal_label = f"processed-{label}"
        if (self.dataset_path.exists() or self.events_path.exists()
                or (directory / f"{label}.manifest.json").exists()
                or any(directory.glob(f"{label}-*.frames.gz"))
                or (directory / f"{causal_label}.manifest.json").exists()
                or any(directory.glob(f"{causal_label}-*.frames.gz"))):
            raise FileExistsError(f"refusing to overwrite capture label {label!r}")
        self._lock = threading.Lock()
        self._closed = False
        self._next_frame_id = 0
        self.events = JsonlSink(self.events_path, append=False)
        self.raw = RawFrameWriter(
            directory, label, limit_bytes=limit_bytes, chunk_bytes=chunk_bytes,
        )
        self.causal = RawFrameWriter(
            directory, causal_label, limit_bytes=limit_bytes, chunk_bytes=chunk_bytes,
        )
        self.events.emit({
            "kind": "run_start", "wall_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(), "label": label,
            "board_hash": board_hash, "runtime": self.runtime,
            "model_identity": self.model_identity,
            "clock_domain": self.clock_domain,
        })

    @classmethod
    def from_env(
        cls, *, board_hash: str, runtime: Mapping[str, object],
    ) -> PaperCapture | None:
        directory = os.environ.get("PAPER_CAPTURE_DIR")
        if not directory:
            return None
        label = os.environ.get("PAPER_CAPTURE_LABEL")
        if not label:
            raise RuntimeError("PAPER_CAPTURE_LABEL is required with PAPER_CAPTURE_DIR")
        try:
            limit_gb = float(os.environ.get("PAPER_CAPTURE_LIMIT_GB", "2"))
            chunk_mb = float(os.environ.get("PAPER_CAPTURE_CHUNK_MB", "256"))
        except ValueError as exc:
            raise RuntimeError("paper capture byte limits are invalid") from exc
        if limit_gb <= 0 or chunk_mb <= 0:
            raise RuntimeError("paper capture byte limits must be positive")
        return cls(
            pathlib.Path(directory), label, board_hash=board_hash, runtime=runtime,
            limit_bytes=int(limit_gb * 1024 ** 3),
            chunk_bytes=int(chunk_mb * 1024 ** 2),
        )

    def _emit(self, row: dict[str, object]) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("paper capture is closed")
            self.events.emit(row)

    def frame_sink(self, wall_ns: int, monotonic_ns: int, raw: str | bytes) -> int:
        with self._lock:
            if self._closed:
                raise RuntimeError("paper capture is closed")
            frame_id = self._next_frame_id
            self._next_frame_id += 1
            if not self.raw.submit(wall_ns, raw, monotonic_ns=monotonic_ns):
                raise CaptureWriteError(
                    f"paper raw capture cannot accept frame: {self.raw.snapshot()}"
                )
            return frame_id

    def processed_event(
        self, wall_ns: int, monotonic_ns: int, frame_id: int, event_index: int,
    ) -> None:
        if frame_id < 0 or event_index < 0:
            raise ValueError("processed event identifiers must be non-negative")
        payload = CAUSAL_EVENT_RECORD.pack(CAUSAL_EVENT, frame_id, event_index)
        with self._lock:
            if self._closed:
                raise RuntimeError("paper capture is closed")
            if not self.causal.submit(
                wall_ns, payload, monotonic_ns=monotonic_ns,
            ):
                raise CaptureWriteError(
                    "paper causal capture cannot accept processed event"
                )

    def quote_tick(self, wall_ns: int, monotonic_ns: int) -> None:
        payload = CAUSAL_TICK_RECORD.pack(CAUSAL_TICK)
        with self._lock:
            if self._closed:
                raise RuntimeError("paper capture is closed")
            if not self.causal.submit(
                wall_ns, payload, monotonic_ns=monotonic_ns,
            ):
                raise CaptureWriteError("paper causal capture cannot accept quote tick")

    def market_open(self, market: ActiveMarket, observed_at: float) -> None:
        self._emit({
            "kind": "market_open", "wall_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(), "observed_at": observed_at,
            "asset": market.asset, "slug": market.slug, "start": market.start,
            "condition_id": market.condition_id, "up_token": market.up_token,
            "down_token": market.down_token, "min_order_size": market.min_order_size,
        })

    def connection(
        self, connected: bool, *, reason: str | None = None,
        observed_at: float | None = None,
    ) -> None:
        self._emit({
            "kind": "connection" if connected else "disconnect",
            "wall_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(),
            "observed_at": time.time() if observed_at is None else observed_at,
            "reason": reason,
        })

    def connection_failure(self, *, reason: str) -> None:
        self._emit({
            "kind": "connection_failure", "wall_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(), "reason": reason,
        })

    def transport_liveness(self, kind: str, wall_ns: int, monotonic_ns: int) -> None:
        if kind not in {"transport_ping", "transport_pong"}:
            raise ValueError("unknown transport liveness marker")
        if wall_ns < 0 or monotonic_ns < 0:
            raise ValueError("transport liveness timestamps must be non-negative")
        self._emit({
            "kind": kind, "wall_ns": wall_ns, "monotonic_ns": monotonic_ns,
        })

    def market_finish(
        self, asset: str, slug: str, start: int, observed_at: float,
    ) -> None:
        self._emit({
            "kind": "market_finish", "wall_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(), "observed_at": observed_at,
            "asset": asset, "slug": slug, "start": start,
        })

    def resolution(self, window: ResolvedWindow, observed_at: float) -> None:
        self._emit({
            "kind": "resolution", "wall_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(), "observed_at": observed_at,
            "asset": window.asset, "slug": window.slug, "start": window.start,
            "condition_id": window.condition_id, "up_token": window.up_token,
            "down_token": window.down_token, "winner_up": window.winner_up,
        })

    def snapshot(self) -> Mapping[str, object]:
        return {"raw": self.raw.snapshot(), "causal": self.causal.snapshot()}

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.raw.close()
            self.causal.close()
            raw_status = self.raw.snapshot()
            causal_status = self.causal.snapshot()
            self.events.emit({
                "kind": "run_end", "wall_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
                "clock_domain": self.clock_domain,
                "raw_status": raw_status, "causal_status": causal_status,
            })
            self.events.close()
            raw_manifest = pathlib.Path(str(self.raw.manifest_path))
            causal_manifest = pathlib.Path(str(self.causal.manifest_path))
            manifest = {
                "schema": "project-fail-paper-capture-v2",
                "label": self.label,
                "started_at": self.started_at,
                "ended_at": time.time(),
                "board_hash": self.board_hash,
                "runtime": self.runtime,
                "model_identity": self.model_identity,
                "clock_domain": self.clock_domain,
                "raw": {"name": raw_manifest.name, "sha256": _sha256(raw_manifest)},
                "causal": {
                    "name": causal_manifest.name, "sha256": _sha256(causal_manifest),
                },
                "events": {"name": self.events_path.name,
                           "sha256": _sha256(self.events_path)},
                "raw_status": raw_status,
                "causal_status": causal_status,
            }
            temporary = self.dataset_path.with_suffix(self.dataset_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            os.replace(temporary, self.dataset_path)
