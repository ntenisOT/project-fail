from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from paper.capture import PaperCapture
from tools.crossvenue_join import JoinIntegrityError, build_join
from tools.transport_telemetry import RawFrameWriter


SOURCES = ("polymarket_rtds", "binance_spot", "binance_futures", "deribit")
WALLETS = ("0x" + "1" * 40, "0x" + "2" * 40)


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


def _write_paper_events(
    dataset_path: pathlib.Path, dataset: dict[str, object],
    rows: list[dict[str, object]],
) -> None:
    events_meta = dataset["events"]
    assert isinstance(events_meta, dict)
    events_path = dataset_path.parent / str(events_meta["name"])
    events_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    events_meta["sha256"] = _sha256(events_path)
    dataset_path.write_text(json.dumps(dataset, sort_keys=True), encoding="utf-8")


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
            "kind": "source_connection_failure", "source": SOURCES[0],
            "connection_id": 2, "wall_ns": start_wall + 5_500_000_000,
            "monotonic_ns": start_mono + 5_500_000_000, "error": "retry",
        }, {
            "kind": "source_connected", "source": SOURCES[0], "connection_id": 3,
            "wall_ns": start_wall + 6_000_000_000,
            "monotonic_ns": start_mono + 6_000_000_000,
        }))
    for index, source in enumerate(SOURCES, 1):
        rows.extend(({
            "kind": "source_final", "source": source,
            "attempts": 3 if reconnect and source == SOURCES[0] else 1,
            "connections": 2 if reconnect and source == SOURCES[0] else 1,
            "reconnects": 2 if reconnect and source == SOURCES[0] else 0,
            "disconnects": 1 if reconnect and source == SOURCES[0] else 0,
            "preconnect_failures": 1 if reconnect and source == SOURCES[0] else 0,
            "wall_ns": end_wall - index,
            "monotonic_ns": end_mono - index,
        }, {
            "kind": "raw_final", "source": source, "raw": raw_statuses[source],
            "wall_ns": end_wall - index + 1,
            "monotonic_ns": end_mono - index + 1,
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


def _artifacts(
    tmp_path: pathlib.Path, events: list[dict[str, object]],
) -> dict[str, pathlib.Path]:
    start_ns, end_ns = int(events[0]["wall_ns"]), int(events[-1]["wall_ns"])
    window_ns = 300_000_000_000
    first = ((start_ns + window_ns - 1) // window_ns * window_ns) // 1_000_000_000
    last = ((end_ns - window_ns) // window_ns * window_ns) // 1_000_000_000
    starts = list(range(first, last + 1, 300))
    market_rows = [{
        "slug": f"btc-updown-5m-{start}", "asset": "btc", "start": start,
        "condition_id": "0x" + f"{start:x}".zfill(64),
        "up_token": f"{start}1", "down_token": f"{start}2", "winner_up": 1,
    } for start in starts]
    cohort = {
        "schema": "project-fail-frozen-wallet-cohort-v1",
        "wallets": list(WALLETS),
        "period": {"start": first - 1200, "end": first - 900},
        "discovery_end": first - 1200, "holdout_start": first - 900,
        "selection": "frozen pre-period activity cohort",
        "selection_sources": {wallet: ["activity"] for wallet in WALLETS},
    }
    fill = {
        "wallet": WALLETS[0], "slug": market_rows[0]["slug"],
        "token": market_rows[0]["up_token"], "side": 1,
        "block_number": 123, "log_index": 4, "block_ts": first + 1,
        "role": "maker", "size": 5.0, "price": 0.49, "fee": 0.0,
        "tx_hash": "0x" + "a" * 64,
    }
    gamma = {
        "schema": "project-fail-gamma-resolution-regimes-v1",
        "rows": [{
            "slug": row["slug"], "resolution_source": "chainlink",
            "lookback_s": 60, "config_id": "btc-twap-60",
            "price_to_beat": "100", "final_price": "101",
        } for row in market_rows],
    }
    payloads = {
        "cohort": json.dumps(cohort),
        "wallet_fills": json.dumps(fill) + "\n",
        "markets": "".join(json.dumps(row) + "\n" for row in market_rows),
        "gamma": json.dumps(gamma),
    }
    result: dict[str, pathlib.Path] = {}
    for name, payload in payloads.items():
        path = tmp_path / f"{name}.json"
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


def _downgrade_source_finals_to_exact_v1(path: pathlib.Path) -> None:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    telemetry = path.parent / dataset["telemetry"]["path"]
    rows = [json.loads(line) for line in telemetry.read_text().splitlines()]
    rows = [
        row for row in rows
        if not (row.get("kind") == "source_connected"
                and row.get("source") == SOURCES[0]
                and row.get("connection_id") == 1)
    ]
    for row in rows:
        if row.get("kind") == "source_connection_failure":
            row["kind"] = "source_closed"
        if row.get("kind") == "source_final":
            row["connections"] = row["attempts"]
            row.pop("attempts")
            row.pop("disconnects")
            row.pop("preconnect_failures")
    telemetry.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    dataset["telemetry"]["sha256"] = _sha256(telemetry)
    path.write_text(json.dumps(dataset, sort_keys=True), encoding="utf-8")


def _set_duplicate_source_spec(path: pathlib.Path, present: bool) -> None:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    telemetry = path.parent / dataset["telemetry"]["path"]
    rows = [json.loads(line) for line in telemetry.read_text().splitlines()]
    start = next(row for row in rows if row.get("kind") == "capture_start")
    sources = start["sources"]
    if present:
        sources.append(dict(sources[0]))
    else:
        sources.pop()
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
    artifacts = _artifacts(tmp_path, events)
    report = build_join(paper, cross, artifacts, "analysis-deadbeef")

    assert report["clock_evidence"]["mode"] == "explicit_host_boot"
    assert report["overlap"]["duration_s"] == 600
    assert set(report["crossvenue"]["sources"]) == set(SOURCES)
    gaps = report["crossvenue"]["disconnect_gaps"]
    assert len(gaps) == 9
    retry_chain = [gap for gap in gaps if gap["source"] == SOURCES[0]
                   and gap["start_kind"] == "source_closed"]
    assert len(retry_chain) == 1
    assert retry_chain[0]["marker_kinds"] == [
        "source_closed", "source_connection_failure",
    ]
    assert report["passive_artifacts"]["gamma"]["validation"]["lookback_s"] == [60]
    assert report["passive_artifacts"]["wallet_fills"]["validation"]["inactive_wallets"] == 1
    assert report["feature_join_contract"]["hardcoded_twap_regime"] is False
    assert report["paper"]["book_chain_gaps"] is None
    assert report["paper"]["book_chain_gap_status"] == (
        "not_materialized_event_study_no_go"
    )

    _downgrade_source_finals_to_exact_v1(cross)
    exact_v1 = build_join(paper, cross, artifacts, "analysis-deadbeef")
    assert {
        source["lifecycle_schema"]
        for source in exact_v1["crossvenue"]["sources"].values()
    } == {"exact-lifecycle-v1"}

    fill_path = artifacts["wallet_fills"]
    fill = json.loads(fill_path.read_text(encoding="utf-8"))
    missing_fee = dict(fill)
    missing_fee.pop("fee")
    fill_path.write_text(json.dumps(missing_fee) + "\n", encoding="utf-8")
    with pytest.raises(JoinIntegrityError, match="lacks required fields"):
        build_join(paper, cross, artifacts, "analysis-deadbeef")

    fill["wallet"] = "0x" + "3" * 40
    fill_path.write_text(json.dumps(fill) + "\n", encoding="utf-8")
    with pytest.raises(JoinIntegrityError, match="outside frozen cohort"):
        build_join(paper, cross, artifacts, "analysis-deadbeef")

    for bad_price, error in (("0", "incomplete Gamma"), ("NaN", "non-finite")):
        artifacts = _artifacts(tmp_path, events)
        gamma_path = artifacts["gamma"]
        gamma = json.loads(gamma_path.read_text(encoding="utf-8"))
        gamma["rows"][0]["price_to_beat"] = bad_price
        gamma_path.write_text(json.dumps(gamma), encoding="utf-8")
        with pytest.raises(JoinIntegrityError, match=error):
            build_join(paper, cross, artifacts, "analysis-deadbeef")

    for reused_field in ("condition_id", "up_token"):
        artifacts = _artifacts(tmp_path, events)
        markets_path = artifacts["markets"]
        markets = [json.loads(line) for line in markets_path.read_text().splitlines()]
        extra = dict(markets[0])
        extra["start"] += 300
        extra["slug"] = f"btc-updown-5m-{extra['start']}"
        extra["condition_id"] = "0x" + "f" * 64
        extra["up_token"] = f"{extra['start']}1"
        extra["down_token"] = f"{extra['start']}2"
        extra[reused_field] = markets[0][reused_field]
        markets_path.write_text(
            "".join(json.dumps(row) + "\n" for row in [*markets, extra]),
            encoding="utf-8",
        )
        with pytest.raises(JoinIntegrityError, match="invalid market mapping"):
            build_join(paper, cross, artifacts, "analysis-deadbeef")

    artifacts = _artifacts(tmp_path, events)
    _set_duplicate_source_spec(cross, True)
    with pytest.raises(JoinIntegrityError, match="source set is incomplete"):
        build_join(paper, cross, artifacts, "analysis-deadbeef")
    _set_duplicate_source_spec(cross, False)

    _rewrite_cross_clock(cross, "0" * 64)
    with pytest.raises(JoinIntegrityError, match="clock domains differ"):
        build_join(paper, cross, _artifacts(tmp_path, events), "analysis-deadbeef")


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
    paper_events.insert(-1, {
        "kind": "connection", "wall_ns": int(paper_events[-1]["wall_ns"]) - 2,
        "monotonic_ns": int(paper_events[-1]["monotonic_ns"]) - 2,
    })
    _write_paper_events(paper, paper_data, paper_events)

    mixed = build_join(paper, cross, _artifacts(tmp_path, events), "analysis-deadbeef")
    assert mixed["clock_evidence"]["mode"] == "mixed_inferred_to_explicit_host_boot"
    assert mixed["clock_evidence"]["identity"] == events[0]["clock_domain"]
    assert mixed["clock_evidence"]["explicit_identity_source"] == "crossvenue"

    paper_events.insert(-1, {
        "kind": "disconnect",
        "wall_ns": int(paper_events[-1]["wall_ns"]) - 1,
        "monotonic_ns": int(paper_events[-1]["monotonic_ns"]) - 1,
    })
    _write_paper_events(paper, paper_data, paper_events)
    with pytest.raises(JoinIntegrityError, match="legacy paper transport gap"):
        build_join(paper, cross, _artifacts(tmp_path, events), "analysis-deadbeef")
    paper_events.pop(-2)
    _write_paper_events(paper, paper_data, paper_events)

    cross_data = json.loads(cross.read_text(encoding="utf-8"))
    telemetry = cross.parent / cross_data["telemetry"]["path"]
    cross_rows = [json.loads(line) for line in telemetry.read_text().splitlines()]
    cross_data.pop("clock_domain")
    for row in cross_rows:
        row.pop("clock_domain", None)
        if row.get("kind") in {"source_connected", "source_final", "raw_final"}:
            row.pop("wall_ns", None)
            row.pop("monotonic_ns", None)
    telemetry.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in cross_rows),
        encoding="utf-8",
    )
    cross_data["telemetry"]["sha256"] = _sha256(telemetry)
    cross.write_text(json.dumps(cross_data, sort_keys=True), encoding="utf-8")

    report = build_join(paper, cross, _artifacts(tmp_path, events), "analysis-deadbeef")
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
        build_join(paper, cross, _artifacts(tmp_path, events), "analysis-deadbeef")
