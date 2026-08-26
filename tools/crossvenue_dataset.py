"""Strict reader for finalized cross-venue capture trees."""

from __future__ import annotations

import hashlib
import json
import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tools.crossvenue_gaps import GapIntegrityError, unavailable_gaps
from tools.transport_telemetry import validate_clock_domain, validated_revision


REQUIRED_SOURCES = frozenset({
    "polymarket_rtds", "binance_spot", "binance_futures", "deribit",
})


class JoinIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class CrossDataset:
    path: pathlib.Path
    sha256: str
    label: str
    asset: str
    revision: str
    clock_domain: Mapping[str, str] | None
    start: Mapping[str, object]
    end: Mapping[str, object]
    sources: Mapping[str, Mapping[str, object]]
    gaps: tuple[Mapping[str, object], ...]


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _object(path: pathlib.Path, kind: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JoinIntegrityError(f"cannot read {kind} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise JoinIntegrityError(f"{kind} is not an object")
    return value


def _child(parent: pathlib.Path, value: object, kind: str) -> pathlib.Path:
    name = str(value or "")
    candidate = pathlib.Path(name)
    if not name or candidate.is_absolute():
        raise JoinIntegrityError(f"invalid {kind} path")
    root, resolved = parent.resolve(), (parent / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise JoinIntegrityError(f"{kind} escapes its dataset directory") from exc
    return resolved


def count_value(value: object, field: str) -> int:
    try:
        result = int(str(value))
    except ValueError as exc:
        raise JoinIntegrityError(f"invalid {field}") from exc
    if result < 0:
        raise JoinIntegrityError(f"negative {field}")
    return result


def verified_raw_manifest(path: pathlib.Path) -> dict[str, object]:
    value = _object(path, "raw manifest")
    if (value.get("schema") != "project-fail-raw-frames-v2"
            or value.get("record_header") != "!QQI"):
        raise JoinIntegrityError(f"unsupported raw manifest: {path}")
    for field, expected in (("dropped_frames", 0), ("capped", False),
                            ("error", None)):
        if value.get(field) != expected:
            raise JoinIntegrityError(f"raw capture is incomplete: {field}")
    accepted_bytes = count_value(value.get("accepted_bytes"), "accepted_bytes")
    written_bytes = count_value(value.get("written_bytes"), "written_bytes")
    accepted_frames = count_value(value.get("accepted_frames"), "accepted_frames")
    written_frames = count_value(value.get("written_frames"), "written_frames")
    if (accepted_bytes != written_bytes or accepted_frames != written_frames
            or accepted_frames == 0):
        raise JoinIntegrityError("raw accepted/written counts are incomplete")
    files = value.get("files")
    if not isinstance(files, list) or not files:
        raise JoinIntegrityError("raw manifest has no chunks")
    disk_bytes = 0
    for index, row in enumerate(files):
        if not isinstance(row, dict):
            raise JoinIntegrityError(f"invalid raw chunk {index}")
        name = str(row.get("name") or "")
        if pathlib.Path(name).name != name:
            raise JoinIntegrityError(f"invalid raw chunk name: {name}")
        chunk = _child(path.parent, name, "raw chunk")
        size = count_value(row.get("bytes"), f"raw chunk {name} bytes")
        try:
            if chunk.stat().st_size != size or file_sha256(chunk) != row.get("sha256"):
                raise JoinIntegrityError(f"raw chunk mismatch: {name}")
        except OSError as exc:
            raise JoinIntegrityError(f"raw chunk is missing: {name}") from exc
        disk_bytes += size
    return {
        "path": path.as_posix(), "sha256": file_sha256(path),
        "accepted_bytes": accepted_bytes, "accepted_frames": accepted_frames,
        "disk_bytes": disk_bytes, "chunks": len(files),
    }


def _rows(path: pathlib.Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise JoinIntegrityError(
                        f"telemetry row {line_number} is not an object"
                    )
                rows.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise JoinIntegrityError(f"invalid cross-venue telemetry: {exc}") from exc
    return rows


def _one(rows: Sequence[Mapping[str, object]], kind: str) -> Mapping[str, object]:
    selected = [row for row in rows if row.get("kind") == kind]
    if len(selected) != 1:
        raise JoinIntegrityError(f"cross-venue telemetry requires one {kind}")
    return selected[0]


def _clock(value: object, kind: str) -> Mapping[str, str] | None:
    if value is None:
        return None
    try:
        return validate_clock_domain(value)
    except ValueError as exc:
        raise JoinIntegrityError(f"invalid {kind} clock domain: {exc}") from exc


def _raw_status_matches(
    status: object, summary: Mapping[str, object], source: str,
) -> None:
    if not isinstance(status, dict):
        raise JoinIntegrityError(f"{source} has no final raw status")
    pairs = (
        ("accepted_bytes", "accepted_bytes"),
        ("written_bytes", "accepted_bytes"),
        ("accepted_frames", "accepted_frames"),
        ("written_frames", "accepted_frames"),
        ("disk_bytes", "disk_bytes"),
    )
    if any(count_value(status.get(left), left) != summary[right]
           for left, right in pairs):
        raise JoinIntegrityError(f"{source} final raw status does not match manifest")
    for field, expected in (("dropped_frames", 0), ("capped", False),
                            ("error", None)):
        if status.get(field) != expected:
            raise JoinIntegrityError(f"{source} final raw status is incomplete")


def _exact_marker(
    row: Mapping[str, object], kind: str, start: tuple[int, int], end: tuple[int, int],
) -> None:
    wall = count_value(row.get("wall_ns"), f"{kind} wall_ns")
    monotonic = count_value(row.get("monotonic_ns"), f"{kind} monotonic_ns")
    if not start[0] <= wall <= end[0] or not start[1] <= monotonic <= end[1]:
        raise JoinIntegrityError(f"{kind} timestamp falls outside capture boundaries")


def load_cross_dataset(path: str | pathlib.Path) -> CrossDataset:
    target = pathlib.Path(path)
    value = _object(target, "cross-venue dataset")
    if value.get("schema") != "project-fail-crossvenue-dataset-v1":
        raise JoinIntegrityError("unsupported cross-venue dataset schema")
    label, asset = str(value.get("label") or ""), str(value.get("asset") or "").lower()
    try:
        revision = validated_revision(str(value.get("revision") or ""))
    except ValueError as exc:
        raise JoinIntegrityError(f"invalid cross-venue revision: {exc}") from exc
    if not label or asset != "btc":
        raise JoinIntegrityError("cross-venue dataset must identify a BTC run")
    clock_domain = _clock(value.get("clock_domain"), "dataset")

    telemetry_meta = value.get("telemetry")
    if not isinstance(telemetry_meta, dict):
        raise JoinIntegrityError("cross-venue dataset lacks telemetry")
    telemetry = _child(target.parent, telemetry_meta.get("path"), "telemetry")
    try:
        if file_sha256(telemetry) != telemetry_meta.get("sha256"):
            raise JoinIntegrityError("cross-venue telemetry hash mismatch")
    except OSError as exc:
        raise JoinIntegrityError("cross-venue telemetry is missing") from exc
    rows = _rows(telemetry)
    start, end = _one(rows, "capture_start"), _one(rows, "capture_end")
    if not rows or rows[0] != start or rows[-1] != end:
        raise JoinIntegrityError("cross-venue run boundaries are out of order")
    if (start.get("label") != label or start.get("asset") != asset
            or start.get("revision") != revision or end.get("label") != label):
        raise JoinIntegrityError("cross-venue run identity does not match boundaries")
    if not (clock_domain == _clock(start.get("clock_domain"), "capture_start")
            == _clock(end.get("clock_domain"), "capture_end")):
        raise JoinIntegrityError("cross-venue clock identity does not match boundaries")
    specs = start.get("sources")
    if (not isinstance(specs, list) or len(specs) != len(REQUIRED_SOURCES)
            or any(not isinstance(row, dict) for row in specs)
            or {str(row.get("name")) for row in specs} != REQUIRED_SOURCES):
        raise JoinIntegrityError("cross-venue source set is incomplete")

    raw_entries = value.get("raw_manifests")
    if not isinstance(raw_entries, list):
        raise JoinIntegrityError("cross-venue dataset lacks raw manifests")
    summaries: dict[str, Mapping[str, object]] = {}
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise JoinIntegrityError("invalid cross-venue raw-manifest entry")
        source = str(entry.get("source") or "")
        if source in summaries:
            raise JoinIntegrityError(f"duplicate raw source: {source}")
        manifest = _child(target.parent, entry.get("path"), "raw manifest")
        try:
            if file_sha256(manifest) != entry.get("sha256"):
                raise JoinIntegrityError(f"{source} raw-manifest hash mismatch")
        except OSError as exc:
            raise JoinIntegrityError(f"{source} raw manifest is missing") from exc
        summaries[source] = verified_raw_manifest(manifest)
    if set(summaries) != REQUIRED_SOURCES:
        raise JoinIntegrityError("cross-venue raw source set is incomplete")

    source_finals = [row for row in rows if row.get("kind") == "source_final"]
    raw_finals = [row for row in rows if row.get("kind") == "raw_final"]
    if ({str(row.get("source")) for row in source_finals} != REQUIRED_SOURCES
            or {str(row.get("source")) for row in raw_finals} != REQUIRED_SOURCES
            or len(source_finals) != 4 or len(raw_finals) != 4):
        raise JoinIntegrityError("cross-venue final source records are incomplete")
    for row in raw_finals:
        source = str(row["source"])
        _raw_status_matches(row.get("raw"), summaries[source], source)

    closes = [row for row in rows if row.get("kind") == "source_closed"]
    failures = [row for row in rows if row.get("kind") == "source_connection_failure"]
    connections = [row for row in rows if row.get("kind") == "source_connected"]
    if clock_domain is not None:
        capture_start = (
            count_value(start.get("wall_ns"), "capture_start wall_ns"),
            count_value(start.get("monotonic_ns"), "capture_start monotonic_ns"),
        )
        capture_end = (
            count_value(end.get("wall_ns"), "capture_end wall_ns"),
            count_value(end.get("monotonic_ns"), "capture_end monotonic_ns"),
        )
        for row in [*connections, *closes, *failures, *source_finals, *raw_finals]:
            _exact_marker(row, str(row.get("kind")), capture_start, capture_end)
        positions = {id(row): index for index, row in enumerate(rows)}
        for source in REQUIRED_SOURCES:
            source_connected = [row for row in connections if row.get("source") == source]
            source_closed = [row for row in closes if row.get("source") == source]
            source_failed = [row for row in failures if row.get("source") == source]
            final = next(row for row in source_finals if row.get("source") == source)
            raw_final = next(row for row in raw_finals if row.get("source") == source)
            reconnects = count_value(final.get("reconnects"), f"{source} reconnects")
            connected_id_rows = [
                count_value(row.get("connection_id"), "connection_id")
                for row in source_connected
            ]
            closed_id_rows = [
                count_value(row.get("connection_id"), "connection_id")
                for row in source_closed
            ]
            failed_id_rows = [
                count_value(row.get("connection_id"), "connection_id")
                for row in source_failed
            ]
            connected_ids, closed_ids = set(connected_id_rows), set(closed_id_rows)
            rich_fields = ("attempts", "disconnects", "preconnect_failures")
            rich_present = [field in final for field in rich_fields]
            if any(rich_present) and not all(rich_present):
                raise JoinIntegrityError(f"{source} connection continuity is invalid")
            if all(rich_present):
                attempts = count_value(final.get("attempts"), f"{source} attempts")
                n_connections = count_value(
                    final.get("connections"), f"{source} connections",
                )
                disconnects = count_value(
                    final.get("disconnects"), f"{source} disconnects",
                )
                preconnect = count_value(
                    final.get("preconnect_failures"), f"{source} preconnect failures",
                )
                attempt_ids = connected_id_rows + failed_id_rows
                if (attempts == 0
                        or sorted(attempt_ids) != list(range(1, attempts + 1))
                        or n_connections != len(source_connected)
                        or disconnects != len(source_closed)
                        or preconnect != len(source_failed)
                        or reconnects != len(source_closed) + len(source_failed)
                        or len(connected_ids) != len(connected_id_rows)
                        or len(closed_ids) != len(closed_id_rows)
                        or not closed_ids <= connected_ids):
                    raise JoinIntegrityError(f"{source} connection continuity is invalid")
                lifecycle_schema = "exact-lifecycle-v2"
            else:
                attempts = count_value(
                    final.get("connections"), f"{source} legacy attempts",
                )
                observed_ids = connected_ids | closed_ids
                if (attempts == 0 or source_failed
                        or observed_ids != set(range(1, attempts + 1))
                        or len(connected_ids) != len(connected_id_rows)
                        or len(closed_ids) != len(closed_id_rows)
                        or reconnects != len(source_closed)):
                    raise JoinIntegrityError(f"{source} connection continuity is invalid")
                lifecycle_schema = "exact-lifecycle-v1"
            for connection_id in connected_ids:
                if connection_id < attempts and connection_id not in closed_ids:
                    raise JoinIntegrityError(f"{source} connection continuity is invalid")
            source_lifecycle = [row for row in rows if row.get("source") == source
                                and row.get("kind") in {
                                    "source_connected", "source_closed",
                                    "source_connection_failure",
                                }]
            ordered_ids = [count_value(row.get("connection_id"), "connection_id")
                           for row in source_lifecycle]
            connected_by_id = dict(zip(connected_id_rows, source_connected, strict=True))
            closed_by_id = dict(zip(closed_id_rows, source_closed, strict=True))
            bad_close_order = any(
                positions[id(connected_by_id[key])] >= positions[id(closed_by_id[key])]
                for key in connected_ids & closed_ids)
            if ordered_ids != sorted(ordered_ids) or bad_close_order:
                raise JoinIntegrityError(f"{source} connection marker order is invalid")
            last_lifecycle = max(
                positions[id(row)]
                for row in [*source_connected, *source_closed, *source_failed]
            )
            if positions[id(final)] <= last_lifecycle or positions[id(raw_final)] <= positions[id(final)]:
                raise JoinIntegrityError(f"{source} final marker order is invalid")
            summaries[source] = {**summaries[source], "lifecycle_schema": lifecycle_schema}
    elif closes or failures:
        raise JoinIntegrityError("legacy cross-venue reconnects lack exact timestamps")
    for row in source_finals:
        source = str(row["source"])
        reconnects = count_value(row.get("reconnects"), f"{source} reconnects")
        observed = sum(str(item.get("source")) == source for item in [*closes, *failures])
        if reconnects != observed:
            raise JoinIntegrityError(f"{source} reconnect count mismatch")

    try:
        gaps = (() if clock_domain is None
                else unavailable_gaps(rows, start, end, REQUIRED_SOURCES))
    except GapIntegrityError as exc:
        raise JoinIntegrityError(str(exc)) from exc
    return CrossDataset(
        target, file_sha256(target), label, asset, revision, clock_domain, start, end,
        summaries, gaps,
    )
