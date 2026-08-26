from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from paper.capture import PaperCapture
from tools.crossvenue_join import JoinIntegrityError, build_join
from tools.transport_telemetry import RawFrameWriter


SOURCES = ("polymarket_rtds", "binance_spot", "binance_futures", "deribit")


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _paper(tmp_path: pathlib.Path) -> tuple[pathlib.Path, list[dict[str, object]]]:
    capture = PaperCapture(
        tmp_path / "paper", "paper", board_hash="board",
        runtime={"assets": ["btc"]}, limit_bytes=10_000, chunk_bytes=10_000,
    )
    capture.frame_sink(101, 201, b"{}")
    capture.quote_tick(102, 202)
    capture.close()
    dataset_path = tmp_path / "paper" / "paper.dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    events_path = tmp_path / "paper" / "paper.events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    events[-1]["wall_ns"] = events[0]["wall_ns"] + 600_000_000_000
    events[-1]["monotonic_ns"] = events[0]["monotonic_ns"] + 600_000_000_000
    events_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in events),
        encoding="utf-8",
    )
    dataset["events"]["sha256"] = _sha256(events_path)
    dataset["ended_at"] = events[-1]["wall_ns"] / 1_000_000_000
    dataset_path.write_text(json.dumps(dataset, sort_keys=True), encoding="utf-8")
    return dataset_path, events


def _cross(
    tmp_path: pathlib.Path, paper_events: list[dict[str, object]],
    *, reconnect: bool = False,
) -> pathlib.Path:
    root, raw_dir = tmp_path / "cross", tmp_path / "cross" / "raw"
    root.mkdir()
    clock = paper_events[0]["clock_domain"]
    start_wall = int(paper_events[0]["wall_ns"]) - 10_000_000_000
    start_mono = int(paper_events[0]["monotonic_ns"]) - 10_000_000_000
    end_wall = int(paper_events[-1]["wall_ns"]) + 10_000_000_000
    end_mono = int(paper_events[-1]["monotonic_ns"]) + 10_000_000_000
    raw_entries: list[dict[str, object]] = []
    raw_statuses: dict[str, dict[str, object]] = {}
    for source in SOURCES:
        writer = RawFrameWriter(
            raw_dir, f"cross-{source}", limit_bytes=10_000, chunk_bytes=10_000,
        )
        writer.submit(start_wall + 1, b"{}", monotonic_ns=start_mono + 1)
        writer.close()
        manifest = pathlib.Path(str(writer.manifest_path))
        raw_entries.append({
            "source": source, "path": manifest.relative_to(root).as_posix(),
            "sha256": _sha256(manifest),
        })
        raw_statuses[source] = dict(writer.snapshot())
    telemetry = root / "cross.jsonl"
    rows: list[dict[str, object]] = [{
        "kind": "capture_start", "schema": "project-fail-crossvenue-v1",
        "label": "cross", "asset": "btc", "revision": "deadbeef",
        "wall_ns": start_wall, "monotonic_ns": start_mono,
        "clock_domain": clock, "sources": [{"name": name} for name in SOURCES],
    }]
    for index, source in enumerate(SOURCES, 1):
        rows.append({
            "kind": "source_connected", "source": source, "connection_id": 1,
            "wall_ns": start_wall + index, "monotonic_ns": start_mono + index,
        })
    if reconnect:
        rows.extend(({
            "kind": "source_closed", "source": SOURCES[0], "connection_id": 1,
            "wall_ns": start_wall + 5_000_000_000,
            "monotonic_ns": start_mono + 5_000_000_000, "error": "test",
        }, {
            "kind": "source_connected", "source": SOURCES[0], "connection_id": 2,
            "wall_ns": start_wall + 6_000_000_000,
            "monotonic_ns": start_mono + 6_000_000_000,
        }))
    for index, source in enumerate(SOURCES, 1):
        rows.extend(({
            "kind": "source_final", "source": source,
            "connections": 2 if reconnect and source == SOURCES[0] else 1,
            "reconnects": 1 if reconnect and source == SOURCES[0] else 0,
            "wall_ns": end_wall - index,
            "monotonic_ns": end_mono - index,
        }, {
            "kind": "raw_final", "source": source, "raw": raw_statuses[source],
        }))
    rows.append({
        "kind": "capture_end", "label": "cross", "wall_ns": end_wall,
        "monotonic_ns": end_mono, "clock_domain": clock,
    })
    telemetry.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    dataset_path = root / "cross.jsonl.dataset.json"
    dataset_path.write_text(json.dumps({
        "schema": "project-fail-crossvenue-dataset-v1", "label": "cross",
        "asset": "btc", "revision": "deadbeef", "clock_domain": clock,
        "telemetry": {"path": telemetry.name, "sha256": _sha256(telemetry)},
        "raw_manifests": raw_entries,
    }, sort_keys=True), encoding="utf-8")
    return dataset_path


def _artifacts(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    result: dict[str, pathlib.Path] = {}
    for name in ("cohort", "wallet_fills", "markets", "gamma"):
        path = tmp_path / f"{name}.json"
        if name == "markets":
            payload = '{"slug":"btc-updown-5m-1"}\n'
        elif name == "gamma":
            payload = json.dumps({
                "schema": "project-fail-gamma-resolution-regimes-v1",
                "rows": [{
                    "slug": "btc-updown-5m-1", "resolution_source": "chainlink",
                    "lookback_s": 60, "config_id": "btc-twap-60",
                    "price_to_beat": "100", "final_price": "101",
                }],
            })
        else:
            payload = "{}"
        path.write_text(payload, encoding="utf-8")
        result[name] = path
    return result


def _rewrite_cross_clock(path: pathlib.Path, boot_sha256: str) -> None:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    dataset["clock_domain"]["boot_sha256"] = boot_sha256
    telemetry = path.parent / dataset["telemetry"]["path"]
    rows = [json.loads(line) for line in telemetry.read_text().splitlines()]
    for row in rows:
        if "clock_domain" in row:
            row["clock_domain"]["boot_sha256"] = boot_sha256
    telemetry.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    dataset["telemetry"]["sha256"] = _sha256(telemetry)
    path.write_text(json.dumps(dataset, sort_keys=True), encoding="utf-8")


def test_join_binds_complete_datasets_and_rejects_clock_domain_mismatch(
    tmp_path: pathlib.Path,
) -> None:
    paper, events = _paper(tmp_path)
    cross = _cross(tmp_path, events, reconnect=True)
    report = build_join(paper, cross, _artifacts(tmp_path), "analysis-deadbeef")

    assert report["clock_evidence"]["mode"] == "explicit_host_boot"
    assert report["overlap"]["duration_s"] == 600
    assert set(report["crossvenue"]["sources"]) == set(SOURCES)
    assert len(report["crossvenue"]["disconnect_gaps"]) == 1
    assert report["passive_artifacts"]["gamma"]["regimes"]["lookback_s"] == [60]
    assert report["feature_join_contract"]["hardcoded_twap_regime"] is False

    _rewrite_cross_clock(cross, "0" * 64)
    with pytest.raises(JoinIntegrityError, match="clock domains differ"):
        build_join(paper, cross, _artifacts(tmp_path), "analysis-deadbeef")


def test_join_allows_clean_legacy_anchors_but_rejects_uncensored_reconnect(
    tmp_path: pathlib.Path,
) -> None:
    paper, events = _paper(tmp_path)
    cross = _cross(tmp_path, events)
    paper_data = json.loads(paper.read_text(encoding="utf-8"))
    paper_events_path = paper.parent / paper_data["events"]["name"]
    paper_events = [json.loads(line) for line in paper_events_path.read_text().splitlines()]
    paper_data.pop("clock_domain")
    for row in paper_events:
        row.pop("clock_domain", None)
    paper_events_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in paper_events),
        encoding="utf-8",
    )
    paper_data["events"]["sha256"] = _sha256(paper_events_path)
    paper.write_text(json.dumps(paper_data, sort_keys=True), encoding="utf-8")

    cross_data = json.loads(cross.read_text(encoding="utf-8"))
    telemetry = cross.parent / cross_data["telemetry"]["path"]
    cross_rows = [json.loads(line) for line in telemetry.read_text().splitlines()]
    cross_data.pop("clock_domain")
    for row in cross_rows:
        row.pop("clock_domain", None)
        if row.get("kind") in {"source_connected", "source_final"}:
            row.pop("wall_ns", None)
            row.pop("monotonic_ns", None)
    telemetry.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in cross_rows),
        encoding="utf-8",
    )
    cross_data["telemetry"]["sha256"] = _sha256(telemetry)
    cross.write_text(json.dumps(cross_data, sort_keys=True), encoding="utf-8")

    report = build_join(paper, cross, _artifacts(tmp_path), "analysis-deadbeef")
    assert report["clock_evidence"]["mode"] == "inferred_offset"

    cross_rows.insert(2, {
        "kind": "source_closed", "source": SOURCES[0], "connection_id": 1,
        "error": "test",
    })
    next(row for row in cross_rows if row.get("kind") == "source_final"
         and row.get("source") == SOURCES[0])["reconnects"] = 1
    telemetry.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in cross_rows),
        encoding="utf-8",
    )
    cross_data["telemetry"]["sha256"] = _sha256(telemetry)
    cross.write_text(json.dumps(cross_data, sort_keys=True), encoding="utf-8")
    with pytest.raises(JoinIntegrityError, match="reconnects lack exact timestamps"):
        build_join(paper, cross, _artifacts(tmp_path), "analysis-deadbeef")
