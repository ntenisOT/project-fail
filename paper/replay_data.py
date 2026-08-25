"""Strict reader for immutable raw market-feed captures."""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
import pathlib
from collections.abc import Iterator, Mapping

from tools.transport_telemetry import RAW_FRAME_HEADER, RAW_FRAME_MAGIC


class CaptureIntegrityError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class RawFrame:
    wall_ns: int
    monotonic_ns: int
    payload: bytes
    chunk: str
    index: int


@dataclasses.dataclass(frozen=True)
class PaperDataset:
    path: pathlib.Path
    raw_manifest: pathlib.Path
    causal_manifest: pathlib.Path
    events: tuple[dict[str, object], ...]
    board_hash: str
    runtime: Mapping[str, object]
    model_identity: Mapping[str, object]


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(path: pathlib.Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureIntegrityError(f"cannot read capture manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CaptureIntegrityError("capture manifest is not an object")
    if value.get("schema") != "project-fail-raw-frames-v2":
        raise CaptureIntegrityError("unsupported capture schema")
    if value.get("record_header") != "!QQI":
        raise CaptureIntegrityError("capture record header mismatch")
    for key, expected in (("dropped_frames", 0), ("capped", False), ("error", None)):
        if value.get(key) != expected:
            raise CaptureIntegrityError(f"capture is incomplete: {key}={value.get(key)!r}")
    return value


def _child_path(parent: pathlib.Path, value: object, kind: str) -> pathlib.Path:
    name = str(value or "")
    candidate = pathlib.Path(name)
    if not name or candidate.is_absolute() or candidate.name != name:
        raise CaptureIntegrityError(f"invalid paper {kind} filename")
    return parent / candidate


def _status_matches_manifest(
    status: Mapping[str, object], manifest: Mapping[str, object],
    manifest_path: pathlib.Path, kind: str,
) -> None:
    for key in (
        "accepted_bytes", "written_bytes", "accepted_frames", "written_frames",
        "dropped_frames", "capped", "error",
    ):
        if status.get(key) != manifest.get(key):
            raise CaptureIntegrityError(f"paper {kind} status mismatch: {key}")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise CaptureIntegrityError(f"paper {kind} manifest lacks files")
    try:
        disk_bytes = sum(int(str(row["bytes"])) for row in files if isinstance(row, dict))
    except (KeyError, TypeError, ValueError) as exc:
        raise CaptureIntegrityError(f"paper {kind} manifest has invalid file sizes") from exc
    if status.get("disk_bytes") != disk_bytes:
        raise CaptureIntegrityError(f"paper {kind} status mismatch: disk_bytes")
    recorded_manifest = pathlib.Path(str(status.get("manifest") or ""))
    if recorded_manifest.name != manifest_path.name:
        raise CaptureIntegrityError(f"paper {kind} status mismatch: manifest")


def load_paper_dataset(path: str | pathlib.Path) -> PaperDataset:
    target = pathlib.Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureIntegrityError(f"cannot read paper dataset {target}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != "project-fail-paper-capture-v2":
        raise CaptureIntegrityError("unsupported paper dataset schema")
    raw = value.get("raw")
    causal = value.get("causal")
    events_meta = value.get("events")
    runtime = value.get("runtime")
    model_identity = value.get("model_identity")
    raw_status = value.get("raw_status")
    causal_status = value.get("causal_status")
    if (not isinstance(raw, dict) or not isinstance(causal, dict)
            or not isinstance(events_meta, dict) or not isinstance(runtime, dict)
            or not isinstance(model_identity, dict) or not isinstance(raw_status, dict)
            or not isinstance(causal_status, dict)):
        raise CaptureIntegrityError("paper dataset metadata is incomplete")
    raw_path = _child_path(target.parent, raw.get("name"), "raw manifest")
    causal_path = _child_path(target.parent, causal.get("name"), "causal manifest")
    events_path = _child_path(target.parent, events_meta.get("name"), "event marker")
    try:
        if _sha256(raw_path) != raw.get("sha256"):
            raise CaptureIntegrityError("paper raw manifest hash mismatch")
        if _sha256(causal_path) != causal.get("sha256"):
            raise CaptureIntegrityError("paper causal manifest hash mismatch")
        if _sha256(events_path) != events_meta.get("sha256"):
            raise CaptureIntegrityError("paper event marker hash mismatch")
    except OSError as exc:
        raise CaptureIntegrityError(f"paper dataset child is unreadable: {exc}") from exc
    events: list[dict[str, object]] = []
    previous_mono = -1
    try:
        with events_path.open(encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise CaptureIntegrityError(f"invalid marker at line {lineno}")
                monotonic_ns = int(str(row.get("monotonic_ns")))
                if monotonic_ns < previous_mono:
                    raise CaptureIntegrityError("paper markers are not monotonic")
                previous_mono = monotonic_ns
                events.append(row)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CaptureIntegrityError(f"invalid paper event markers: {exc}") from exc
    kinds = [str(row.get("kind")) for row in events]
    if kinds.count("run_start") != 1 or kinds.count("run_end") != 1:
        raise CaptureIntegrityError("paper dataset lacks exact run boundaries")
    if not events or kinds[0] != "run_start" or kinds[-1] != "run_end":
        raise CaptureIntegrityError("paper run boundaries are out of order")
    run_start, run_end = events[0], events[-1]
    board_hash = str(value.get("board_hash") or "")
    if not board_hash or run_start.get("board_hash") != board_hash:
        raise CaptureIntegrityError("paper board identity does not match run_start")
    if run_start.get("runtime") != runtime:
        raise CaptureIntegrityError("paper runtime does not match run_start")
    if run_start.get("model_identity") != model_identity:
        raise CaptureIntegrityError("paper model identity does not match run_start")
    if run_end.get("raw_status") != raw_status:
        raise CaptureIntegrityError("paper raw status does not match run_end")
    if run_end.get("causal_status") != causal_status:
        raise CaptureIntegrityError("paper causal status does not match run_end")
    raw_manifest = _manifest(raw_path)
    causal_manifest = _manifest(causal_path)
    _status_matches_manifest(raw_status, raw_manifest, raw_path, "raw")
    _status_matches_manifest(causal_status, causal_manifest, causal_path, "causal")
    return PaperDataset(
        target, raw_path, causal_path, tuple(events), board_hash, runtime,
        model_identity,
    )


def iter_raw_frames(
    manifest_path: str | pathlib.Path, *, max_frame_bytes: int = 32 * 1024 * 1024,
) -> Iterator[RawFrame]:
    """Validate the manifest/chunks while yielding frames in exact ingress order."""
    if max_frame_bytes <= 0:
        raise ValueError("max_frame_bytes must be positive")
    target = pathlib.Path(manifest_path)
    manifest = _manifest(target)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise CaptureIntegrityError("capture chunk list is invalid")

    label = str(manifest.get("label") or "")
    expected_names = [f"{label}-{index:05d}.frames.gz" for index in range(len(files))]
    actual_names: list[str] = []
    total_bytes = total_frames = 0
    previous_wall = previous_mono = -1
    frame_index = 0
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise CaptureIntegrityError(f"invalid chunk entry {index}")
        name = str(entry.get("name") or "")
        actual_names.append(name)
        chunk = target.parent / name
        try:
            if chunk.stat().st_size != int(str(entry.get("bytes"))):
                raise CaptureIntegrityError(f"chunk size mismatch: {name}")
        except (OSError, ValueError) as exc:
            raise CaptureIntegrityError(f"missing or invalid chunk: {name}") from exc
        if _sha256(chunk) != entry.get("sha256"):
            raise CaptureIntegrityError(f"chunk hash mismatch: {name}")
        try:
            with gzip.open(chunk, "rb") as handle:
                if handle.read(len(RAW_FRAME_MAGIC)) != RAW_FRAME_MAGIC:
                    raise CaptureIntegrityError(f"chunk magic mismatch: {name}")
                while True:
                    header = handle.read(RAW_FRAME_HEADER.size)
                    if not header:
                        break
                    if len(header) != RAW_FRAME_HEADER.size:
                        raise CaptureIntegrityError(f"truncated frame header: {name}")
                    wall_ns, monotonic_ns, size = RAW_FRAME_HEADER.unpack(header)
                    if size > max_frame_bytes:
                        raise CaptureIntegrityError(f"oversized frame in {name}: {size}")
                    payload = handle.read(size)
                    if len(payload) != size:
                        raise CaptureIntegrityError(f"truncated frame payload: {name}")
                    if wall_ns < previous_wall or monotonic_ns < previous_mono:
                        raise CaptureIntegrityError("capture timestamps are not monotonic")
                    previous_wall, previous_mono = wall_ns, monotonic_ns
                    total_bytes += RAW_FRAME_HEADER.size + size
                    total_frames += 1
                    yield RawFrame(wall_ns, monotonic_ns, payload, name, frame_index)
                    frame_index += 1
        except (OSError, EOFError) as exc:
            raise CaptureIntegrityError(f"corrupt gzip chunk {name}: {exc}") from exc

    if actual_names != expected_names:
        raise CaptureIntegrityError("capture chunks are missing, duplicated, or out of order")
    for key, actual in (("accepted_bytes", total_bytes), ("written_bytes", total_bytes),
                        ("accepted_frames", total_frames),
                        ("written_frames", total_frames)):
        try:
            expected = int(str(manifest.get(key)))
        except ValueError as exc:
            raise CaptureIntegrityError(f"invalid manifest count: {key}") from exc
        if expected != actual:
            raise CaptureIntegrityError(
                f"manifest {key} mismatch: expected={expected} actual={actual}"
            )
