from __future__ import annotations

import json

import pytest

from paper.capture import PaperCapture
from paper.market_metadata import ActiveMarket
from paper.replay_data import iter_raw_frames
from tools.market_windows import ResolvedWindow
from tools.transport_telemetry import CaptureWriteError


def test_paper_capture_binds_frames_market_outcome_and_board(tmp_path) -> None:
    capture = PaperCapture(
        tmp_path, "cohort", board_hash="abc", runtime={"latency": 0.065},
        limit_bytes=1_000, chunk_bytes=1_000,
    )
    market = ActiveMarket("btc", "btc-updown-5m-300", 300, "0x" + "1" * 64,
                          "11", "22", 5)
    capture.market_open(market, 301.0)
    capture.connection(True)
    frame_id = capture.frame_sink(10, 20, b'{"event_type":"book"}')
    capture.processed_event(11, 21, frame_id, 0)
    capture.quote_tick(12, 22)
    capture.market_finish(market.asset, market.slug, market.start, 600.0)
    capture.resolution(
        ResolvedWindow(market.slug, market.asset, market.start, market.condition_id,
                       market.up_token, market.down_token, 1),
        601.0,
    )
    capture.close()

    dataset = json.loads((tmp_path / "cohort.dataset.json").read_text())
    events = [json.loads(line) for line in
              (tmp_path / "cohort.events.jsonl").read_text().splitlines()]
    frames = list(iter_raw_frames(tmp_path / dataset["raw"]["name"]))
    causal = list(iter_raw_frames(tmp_path / dataset["causal"]["name"]))

    assert dataset["board_hash"] == "abc"
    assert {row["kind"] for row in events} >= {
        "run_start", "market_open", "market_finish", "connection", "resolution",
        "run_end",
    }
    assert [row.payload for row in frames] == [b'{"event_type":"book"}']
    assert len(causal) == 2
    capture.close()
    with pytest.raises(RuntimeError, match="closed"):
        capture.frame_sink(13, 23, b"{}")


def test_paper_capture_fails_immediately_at_byte_cap(tmp_path) -> None:
    capture = PaperCapture(
        tmp_path, "capped", board_hash="abc", runtime={},
        limit_bytes=30, chunk_bytes=30,
    )

    with pytest.raises(CaptureWriteError, match="cannot accept frame"):
        capture.frame_sink(10, 20, b"x" * 20)

    capture.close()
    dataset = json.loads((tmp_path / "capped.dataset.json").read_text())
    assert dataset["raw_status"]["capped"] is True
