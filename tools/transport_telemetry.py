"""Low-overhead transport telemetry and bounded raw-frame capture."""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
import os
import pathlib
import queue
import socket
import struct
import threading
from typing import TextIO


_TCP_INFO_INTS = (
    "rto_us", "ato_us", "snd_mss", "rcv_mss", "unacked", "sacked",
    "lost", "retrans", "fackets", "last_data_sent_ms", "last_ack_sent_ms",
    "last_data_recv_ms", "last_ack_recv_ms", "pmtu", "rcv_ssthresh",
    "rtt_us", "rttvar_us", "snd_ssthresh", "snd_cwnd", "advmss",
    "reordering", "rcv_rtt_us", "rcv_space", "total_retrans",
)
RAW_FRAME_MAGIC = b"PFRAWV2\n"
RAW_FRAME_HEADER = struct.Struct("!QQI")


class CaptureWriteError(RuntimeError):
    """A bounded raw capture can no longer preserve every submitted frame."""


@dataclasses.dataclass
class FrameCounters:
    frames: int = 0
    payload_bytes: int = 0
    events: int = 0
    pongs: int = 0
    parse_errors: int = 0
    missing_source_time: int = 0
    negative_source_age: int = 0
    stale_events: int = 0
    min_source_age_ms: float | None = None
    max_source_age_ms: float = 0.0
    max_gap_ms: float = 0.0
    last_frame_wall: float | None = None
    last_frame_mono: float | None = None

    def observe_gap(self, now_mono: float) -> None:
        if self.last_frame_mono is not None:
            self.max_gap_ms = max(
                self.max_gap_ms, (now_mono - self.last_frame_mono) * 1000,
            )
        self.last_frame_mono = now_mono

    def snapshot(self, *, reset: bool = False) -> dict[str, int | float | None]:
        result = dataclasses.asdict(self)
        result.pop("last_frame_mono")
        if reset:
            last_wall, last_mono = self.last_frame_wall, self.last_frame_mono
            self.__dict__.update(FrameCounters().__dict__)
            self.last_frame_wall, self.last_frame_mono = last_wall, last_mono
        return result


class JsonlSink:
    """Append-only telemetry sink that refuses accidental truncation."""

    def __init__(self, path: pathlib.Path, *, append: bool) -> None:
        if path.exists() and not append:
            raise FileExistsError(f"refusing to overwrite {path}; use --append")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle: TextIO = path.open("a" if append else "x", encoding="utf-8")
        self._closed = False

    def emit(self, row: dict[str, object]) -> None:
        if self._closed:
            raise RuntimeError("JSONL sink is closed")
        line = json.dumps(row, separators=(",", ":"), sort_keys=True)
        self.handle.write(line + "\n")
        self.handle.flush()
        heartbeat = row.get("heartbeat_index", 0)
        index = heartbeat if isinstance(heartbeat, int) else 0
        if row.get("kind") != "heartbeat" or index % 3 == 0:
            print(line, flush=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.handle.close()


def parse_tcp_info(raw: bytes) -> dict[str, int] | None:
    """Decode the stable Linux ``tcp_info`` prefix without kernel-version coupling."""
    required = 8 + 4 * len(_TCP_INFO_INTS)
    if len(raw) < required:
        return None
    bytes_part = struct.unpack_from("=8B", raw)
    ints = struct.unpack_from(f"={len(_TCP_INFO_INTS)}I", raw, 8)
    result = {
        "state": bytes_part[0],
        "ca_state": bytes_part[1],
        "retransmits": bytes_part[2],
        "probes": bytes_part[3],
        "backoff": bytes_part[4],
        "options": bytes_part[5],
    }
    result.update(dict(zip(_TCP_INFO_INTS, ints, strict=True)))
    return result


def websocket_tcp_info(ws: object) -> dict[str, int] | None:
    """Return Linux TCP_INFO for a websockets connection when the OS exposes it."""
    transport = getattr(ws, "transport", None)
    if transport is None:
        return None
    sock = transport.get_extra_info("socket")
    option = getattr(socket, "TCP_INFO", None)
    if sock is None or option is None:
        return None
    try:
        return parse_tcp_info(sock.getsockopt(socket.IPPROTO_TCP, option, 192))
    except OSError:
        return None


@dataclasses.dataclass
class TcpExtrema:
    samples: int = 0
    unavailable: int = 0
    max_rtt_us: int = 0
    max_rttvar_us: int = 0
    max_unacked: int = 0
    max_lost: int = 0
    max_retrans: int = 0
    max_total_retrans: int = 0
    min_rcv_space: int | None = None

    def observe(self, sample: dict[str, int] | None) -> None:
        if sample is None:
            self.unavailable += 1
            return
        self.samples += 1
        self.max_rtt_us = max(self.max_rtt_us, sample["rtt_us"])
        self.max_rttvar_us = max(self.max_rttvar_us, sample["rttvar_us"])
        self.max_unacked = max(self.max_unacked, sample["unacked"])
        self.max_lost = max(self.max_lost, sample["lost"])
        self.max_retrans = max(self.max_retrans, sample["retrans"])
        self.max_total_retrans = max(
            self.max_total_retrans, sample["total_retrans"],
        )
        space = sample["rcv_space"]
        self.min_rcv_space = space if self.min_rcv_space is None else min(
            self.min_rcv_space, space,
        )

    def snapshot(self, *, reset: bool = False) -> dict[str, int | None]:
        result = dataclasses.asdict(self)
        if reset:
            self.__dict__.update(TcpExtrema().__dict__)
        return result


class RawFrameWriter:
    """Write timestamped frames on a worker thread, rotating at strict byte caps.

    Each file starts with :data:`RAW_FRAME_MAGIC`. Records contain wall-clock
    and monotonic receive nanoseconds plus the exact WebSocket payload.
    """

    def __init__(
        self, directory: pathlib.Path, label: str, *,
        limit_bytes: int, chunk_bytes: int, queue_capacity: int = 8192,
    ) -> None:
        if limit_bytes <= 0 or chunk_bytes <= 0:
            raise ValueError("raw frame byte limits must be positive")
        self.directory = directory
        self.label = label
        self.limit_bytes = limit_bytes
        self.chunk_bytes = min(chunk_bytes, limit_bytes)
        if (directory.exists()
                and (any(directory.glob(f"{label}-*.frames.gz"))
                     or (directory / f"{label}.manifest.json").exists())):
            raise FileExistsError(f"refusing to overwrite raw capture label {label!r}")
        self.queue: queue.Queue[tuple[int, int, bytes] | None] = queue.Queue(
            queue_capacity
        )
        self.accepted_bytes = 0
        self.written_bytes = 0
        self.accepted_frames = 0
        self.written_frames = 0
        self.disk_bytes = 0
        self.dropped_frames = 0
        self.capped = False
        self.error: str | None = None
        self.manifest_path: str | None = None
        self._closed = False
        self._state_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name=f"raw-writer-{label}")
        self._thread.start()

    def submit(
        self, received_ns: int, raw: str | bytes, *, monotonic_ns: int,
    ) -> bool:
        payload = raw.encode() if isinstance(raw, str) else raw
        record_bytes = RAW_FRAME_HEADER.size + len(payload)
        with self._state_lock:
            if self._closed:
                raise RuntimeError("raw frame writer is closed")
            if self.accepted_bytes + record_bytes > self.limit_bytes:
                self.capped = True
                return False
            try:
                self.queue.put_nowait((received_ns, monotonic_ns, payload))
            except queue.Full:
                self.dropped_frames += 1
                return False
            self.accepted_bytes += record_bytes
            self.accepted_frames += 1
            return True

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        while self._thread.is_alive():
            try:
                self.queue.put(None, timeout=0.5)
                break
            except queue.Full:
                continue
        self._thread.join()
        files = sorted(self.directory.glob(f"{self.label}-*.frames.gz"))
        self.disk_bytes = sum(path.stat().st_size for path in files)
        self._write_manifest(files)

    def snapshot(self) -> dict[str, int | bool | str | None]:
        return {
            "accepted_bytes": self.accepted_bytes,
            "written_bytes": self.written_bytes,
            "accepted_frames": self.accepted_frames,
            "written_frames": self.written_frames,
            "disk_bytes": self.disk_bytes,
            "dropped_frames": self.dropped_frames,
            "capped": self.capped,
            "error": self.error,
            "manifest": self.manifest_path,
        }

    def _open(self, index: int) -> gzip.GzipFile:
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = gzip.open(
            self.directory / f"{self.label}-{index:05d}.frames.gz",
            "wb", compresslevel=1,
        )
        handle.write(RAW_FRAME_MAGIC)
        return handle

    @staticmethod
    def _digest(path: pathlib.Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _write_manifest(self, files: list[pathlib.Path]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{self.label}.manifest.json"
        temporary = target.with_suffix(target.suffix + ".tmp")
        manifest = {
            "schema": "project-fail-raw-frames-v2",
            "record_header": "!QQI",
            "label": self.label,
            "accepted_bytes": self.accepted_bytes,
            "written_bytes": self.written_bytes,
            "accepted_frames": self.accepted_frames,
            "written_frames": self.written_frames,
            "dropped_frames": self.dropped_frames,
            "capped": self.capped,
            "error": self.error,
            "files": [
                {"name": path.name, "bytes": path.stat().st_size,
                 "sha256": self._digest(path)}
                for path in files
            ],
        }
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, target)
        self.manifest_path = str(target)

    def _run(self) -> None:
        handle: gzip.GzipFile | None = None
        chunk_size = 0
        index = 0
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    break
                received_ns, monotonic_ns, payload = item
                record = RAW_FRAME_HEADER.pack(
                    received_ns, monotonic_ns, len(payload)
                ) + payload
                if handle is None or chunk_size + len(record) > self.chunk_bytes:
                    if handle is not None:
                        handle.close()
                    handle = self._open(index)
                    index += 1
                    chunk_size = len(RAW_FRAME_MAGIC)
                handle.write(record)
                chunk_size += len(record)
                self.written_bytes += len(record)
                self.written_frames += 1
        except OSError as exc:
            self.error = f"{exc.__class__.__name__}: {exc}"
        finally:
            if handle is not None:
                handle.close()
