from __future__ import annotations

import gzip
import struct

import pytest

from tools.transport_probe import _payload_events
from tools.transport_telemetry import (
    RAW_FRAME_HEADER,
    RAW_FRAME_MAGIC,
    RawFrameWriter,
    parse_tcp_info,
)


def test_tcp_info_parser_decodes_stable_linux_prefix() -> None:
    header = bytes((1, 2, 3, 4, 5, 6, 7, 8))
    values = tuple(range(100, 124))

    row = parse_tcp_info(header + struct.pack("=24I", *values))

    assert row is not None
    assert row["state"] == 1
    assert row["rtt_us"] == 115
    assert row["rcv_space"] == 122
    assert row["total_retrans"] == 123
    assert parse_tcp_info(b"short") is None


def test_raw_writer_rotates_and_preserves_exact_frames(tmp_path) -> None:
    writer = RawFrameWriter(
        tmp_path, "probe", limit_bytes=100, chunk_bytes=20, queue_capacity=4,
    )
    writer.submit(11, "abc", monotonic_ns=21)
    writer.submit(12, b"defg", monotonic_ns=22)
    writer.close()

    files = sorted(tmp_path.glob("*.frames.gz"))
    assert len(files) == 2
    rows = []
    for path in files:
        with gzip.open(path, "rb") as handle:
            raw = handle.read()
        assert raw.startswith(RAW_FRAME_MAGIC)
        offset = len(RAW_FRAME_MAGIC)
        received_ns, monotonic_ns, size = RAW_FRAME_HEADER.unpack(
            raw[offset:offset + RAW_FRAME_HEADER.size]
        )
        offset += RAW_FRAME_HEADER.size
        rows.append((received_ns, monotonic_ns, raw[offset:offset + size]))
    assert rows == [(11, 21, b"abc"), (12, 22, b"defg")]
    assert writer.snapshot()["dropped_frames"] == 0
    assert writer.snapshot()["disk_bytes"] > 0
    assert (tmp_path / "probe.manifest.json").exists()
    with pytest.raises(FileExistsError):
        RawFrameWriter(tmp_path, "probe", limit_bytes=100, chunk_bytes=20)


def test_payload_audit_counts_stale_events_and_parse_failures() -> None:
    raw = '[{"event_type":"price_change","timestamp":"1000"}]'

    events, stale, max_age, min_age, missing, negative, failed = (
        _payload_events(raw, 1000.5)
    )

    assert (events, stale, max_age, min_age, missing, negative, failed) == (
        1, 1, 500.0, 500.0, 0, 0, False,
    )
    assert _payload_events("not-json", 1.5) == (
        0, 0, 0.0, None, 0, 0, True,
    )

    future = '[{"event_type":"book","timestamp":"1001"},{}]'
    assert _payload_events(future, 1000.5)[1:7] == (
        1, 0.0, -500.0, 1, 1, False,
    )
