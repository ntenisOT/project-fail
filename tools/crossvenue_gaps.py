"""Derive exact source-unavailable intervals from lifecycle markers."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence


MARKERS = frozenset({
    "source_connected", "source_closed", "source_connection_failure", "source_final",
})


class GapIntegrityError(ValueError):
    pass


def _point(row: Mapping[str, object], kind: str) -> tuple[int, int]:
    try:
        wall = int(str(row.get("wall_ns")))
        monotonic = int(str(row.get("monotonic_ns")))
    except ValueError as exc:
        raise GapIntegrityError(f"{kind} lacks an exact timestamp") from exc
    if wall < 0 or monotonic < 0:
        raise GapIntegrityError(f"{kind} has a negative timestamp")
    return wall, monotonic


def unavailable_gaps(
    rows: Sequence[Mapping[str, object]], start: Mapping[str, object],
    end: Mapping[str, object], sources: Collection[str],
) -> tuple[Mapping[str, object], ...]:
    capture_start = _point(start, "capture_start")
    capture_end = _point(end, "capture_end")
    gaps: list[Mapping[str, object]] = []
    for source in sorted(sources):
        connected = False
        gap_start = capture_start
        start_kind = "capture_start"
        start_reason = "capture_start"
        marker_kinds: list[str] = []
        previous_wall, previous_mono = capture_start
        source_rows = [row for row in rows if row.get("source") == source
                       and row.get("kind") in MARKERS]
        for row in source_rows:
            kind = str(row["kind"])
            point = _point(row, kind)
            if point[0] < previous_wall or point[1] < previous_mono:
                raise GapIntegrityError(f"{source} lifecycle timestamps are not monotonic")
            previous_wall, previous_mono = point
            if kind == "source_connected":
                if point[1] < gap_start[1]:
                    raise GapIntegrityError(f"{source} availability interval is negative")
                if not connected and point[1] > gap_start[1]:
                    gaps.append({
                        "source": source, "closed_wall_ns": gap_start[0],
                        "resumed_wall_ns": point[0],
                        "closed_monotonic_ns": gap_start[1],
                        "resumed_monotonic_ns": point[1],
                        "start_kind": start_kind, "reason": start_reason,
                        "marker_kinds": list(marker_kinds),
                    })
                connected = True
                marker_kinds.clear()
            elif kind in {"source_closed", "source_connection_failure"}:
                if connected:
                    connected = False
                    gap_start, start_kind = point, kind
                    start_reason = str(row.get("error") or kind)
                    marker_kinds.clear()
                marker_kinds.append(kind)
            elif connected:
                connected = False
                gap_start, start_kind = point, kind
                start_reason = kind
                marker_kinds.clear()
        if not connected:
            if capture_end[1] < gap_start[1]:
                raise GapIntegrityError(f"{source} availability interval is negative")
            if capture_end[1] > gap_start[1]:
                gaps.append({
                    "source": source, "closed_wall_ns": gap_start[0],
                    "resumed_wall_ns": capture_end[0],
                    "closed_monotonic_ns": gap_start[1],
                    "resumed_monotonic_ns": capture_end[1],
                    "start_kind": start_kind, "reason": start_reason,
                    "marker_kinds": list(marker_kinds),
                })
    return tuple(gaps)
