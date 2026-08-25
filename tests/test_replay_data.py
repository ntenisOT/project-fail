from __future__ import annotations

import json

import pytest

from paper.replay_data import CaptureIntegrityError, iter_raw_frames, load_paper_dataset
from tools.transport_telemetry import RawFrameWriter


def test_replay_reader_validates_and_preserves_exact_ingress_order(tmp_path) -> None:
    writer = RawFrameWriter(tmp_path, "btc", limit_bytes=1_000, chunk_bytes=40)
    writer.submit(101, b"first", monotonic_ns=201)
    writer.submit(102, b"second", monotonic_ns=202)
    writer.close()

    rows = list(iter_raw_frames(tmp_path / "btc.manifest.json"))

    assert [(row.wall_ns, row.monotonic_ns, row.payload, row.index) for row in rows] == [
        (101, 201, b"first", 0), (102, 202, b"second", 1),
    ]

    writer.close()
    with pytest.raises(RuntimeError, match="closed"):
        writer.submit(103, b"late", monotonic_ns=203)


def test_replay_reader_rejects_any_declared_capture_loss(tmp_path) -> None:
    writer = RawFrameWriter(tmp_path, "btc", limit_bytes=1_000, chunk_bytes=1_000)
    writer.submit(101, b"frame", monotonic_ns=201)
    writer.close()
    path = tmp_path / "btc.manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["dropped_frames"] = 1
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CaptureIntegrityError, match="incomplete"):
        list(iter_raw_frames(path))


def test_paper_dataset_rejects_marker_tampering(tmp_path) -> None:
    from paper.capture import PaperCapture

    capture = PaperCapture(
        tmp_path, "paper", board_hash="abc", runtime={},
        limit_bytes=1_000, chunk_bytes=1_000,
    )
    capture.frame_sink(101, 201, b"{}")
    capture.close()
    events = tmp_path / "paper.events.jsonl"
    events.write_text(events.read_text() + "{}\n")

    with pytest.raises(CaptureIntegrityError, match="marker hash"):
        load_paper_dataset(tmp_path / "paper.dataset.json")


@pytest.mark.parametrize(
    ("field", "match"),
    (("runtime", "runtime"), ("model_identity", "model identity"),
     ("raw_status", "raw status")),
)
def test_paper_dataset_cross_binds_hashed_run_metadata(
    tmp_path, field: str, match: str,
) -> None:
    from paper.capture import PaperCapture

    capture = PaperCapture(
        tmp_path, field, board_hash="abc", runtime={"clock": 1},
        limit_bytes=1_000, chunk_bytes=1_000,
    )
    capture.frame_sink(101, 201, b"{}")
    capture.quote_tick(102, 202)
    capture.close()
    path = tmp_path / f"{field}.dataset.json"
    dataset = json.loads(path.read_text(encoding="utf-8"))
    if field == "runtime":
        dataset[field]["clock"] = 2
    elif field == "model_identity":
        dataset[field]["sha256"] = "0" * 64
    else:
        dataset[field]["accepted_frames"] += 1
    path.write_text(json.dumps(dataset), encoding="utf-8")

    with pytest.raises(CaptureIntegrityError, match=match):
        load_paper_dataset(path)
