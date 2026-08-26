#!/usr/bin/env python3
"""Capture public reference and external-venue feeds for causal replay."""

from __future__ import annotations

import argparse
import asyncio
import collections
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import sys
import time
from collections.abc import Sequence

import websockets

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.crossvenue_sources import ParsedFrame, SourceSpec, parse_frame, source_specs
from tools.transport_telemetry import (
    CaptureWriteError,
    JsonlSink,
    RawFrameWriter,
    TcpExtrema,
    clock_domain_identity,
    validated_revision,
    websocket_tcp_info,
)


LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _timestamp_fields() -> dict[str, int | float]:
    wall_ns = time.time_ns()
    return {
        "ts": wall_ns / 1_000_000_000,
        "wall_ns": wall_ns,
        "monotonic_ns": time.monotonic_ns(),
    }


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclasses.dataclass
class SourceStats:
    frames: int = 0
    payload_bytes: int = 0
    events: int = 0
    parse_errors: int = 0
    missing_source_time: int = 0
    negative_source_age: int = 0
    max_gap_ms: float = 0.0
    min_source_age_ms: float | None = None
    max_source_age_ms: float | None = None
    max_publish_delay_ms: float = 0.0
    last_frame_mono: float | None = None
    source_ages_ms: collections.deque[float] = dataclasses.field(
        default_factory=lambda: collections.deque(maxlen=8192), repr=False,
    )

    def observe(self, parsed: ParsedFrame, payload_bytes: int,
                received_ns: int, received_mono: float) -> None:
        if self.last_frame_mono is not None:
            self.max_gap_ms = max(
                self.max_gap_ms, (received_mono - self.last_frame_mono) * 1000,
            )
        self.last_frame_mono = received_mono
        self.frames += 1
        self.payload_bytes += payload_bytes
        self.events += parsed.events
        self.parse_errors += int(parsed.parse_error)
        if parsed.events and parsed.source_time_ns is None:
            self.missing_source_time += 1
        if parsed.source_time_ns is not None:
            age_ms = (received_ns - parsed.source_time_ns) / 1_000_000
            if -60_000 <= age_ms <= 60_000:
                self.negative_source_age += int(age_ms < 0)
                self.source_ages_ms.append(age_ms)
                self.min_source_age_ms = (
                    age_ms if self.min_source_age_ms is None else
                    min(self.min_source_age_ms, age_ms)
                )
                self.max_source_age_ms = (
                    age_ms if self.max_source_age_ms is None else
                    max(self.max_source_age_ms, age_ms)
                )
        if parsed.source_time_ns is not None and parsed.publisher_time_ns is not None:
            delay_ms = (parsed.publisher_time_ns - parsed.source_time_ns) / 1_000_000
            if 0 <= delay_ms <= 60_000:
                self.max_publish_delay_ms = max(self.max_publish_delay_ms, delay_ms)

    def snapshot(self) -> dict[str, int | float | None]:
        ages = sorted(self.source_ages_ms)

        def percentile(fraction: float) -> float | None:
            if not ages:
                return None
            return round(ages[min(len(ages) - 1, int((len(ages) - 1) * fraction))], 3)

        return {
            "frames": self.frames,
            "payload_bytes": self.payload_bytes,
            "events": self.events,
            "parse_errors": self.parse_errors,
            "missing_source_time": self.missing_source_time,
            "negative_source_age": self.negative_source_age,
            "source_age_samples": len(ages),
            "source_age_p50_ms": percentile(0.50),
            "source_age_p90_ms": percentile(0.90),
            "source_age_p99_ms": percentile(0.99),
            "min_source_age_ms": (None if self.min_source_age_ms is None else
                                  round(self.min_source_age_ms, 3)),
            "max_source_age_ms": (None if self.max_source_age_ms is None else
                                  round(self.max_source_age_ms, 3)),
            "max_publish_delay_ms": round(self.max_publish_delay_ms, 3),
            "max_gap_ms": round(self.max_gap_ms, 3),
        }


async def _tcp_sampler(ws: object, stop: asyncio.Event,
                       stats: TcpExtrema, cadence_s: float) -> None:
    while not stop.is_set():
        stats.observe(websocket_tcp_info(ws))
        await asyncio.sleep(cadence_s)


async def _connection(
    spec: SourceSpec, args: argparse.Namespace, sink: JsonlSink,
    raw: RawFrameWriter | None, stop_all: asyncio.Event,
    stats: SourceStats, tcp_lifetime: TcpExtrema, connection_id: int,
) -> None:
    connected_mono = time.monotonic()
    async with websockets.connect(
        spec.url, ping_interval=None, open_timeout=12, close_timeout=0.2,
        max_queue=args.max_queue,
    ) as ws:
        if spec.subscribe is not None:
            await ws.send(spec.subscribe)
        sink.emit({
            "kind": "source_connected", **_timestamp_fields(),
            "source": spec.name, "connection_id": connection_id,
        })
        connection_stop = asyncio.Event()
        tcp_task = asyncio.create_task(
            _tcp_sampler(ws, connection_stop, tcp_lifetime, args.tcp_cadence_s)
        )
        next_heartbeat = time.monotonic() + args.heartbeat_s
        next_ping = (time.monotonic() + spec.application_ping_s
                     if spec.application_ping_s is not None else None)
        heartbeat_index = 0
        try:
            while not stop_all.is_set():
                now_mono = time.monotonic()
                if next_ping is not None and now_mono >= next_ping:
                    ping = spec.application_ping
                    if ping is None:
                        raise RuntimeError(f"{spec.name} ping cadence has no payload")
                    await ws.send(ping)
                    next_ping += spec.application_ping_s or 0
                if now_mono >= next_heartbeat:
                    heartbeat_index += 1
                    sink.emit({
                        "kind": "source_heartbeat", "heartbeat_index": heartbeat_index,
                        **_timestamp_fields(), "source": spec.name,
                        "connection_id": connection_id,
                        "connected_s": round(now_mono - connected_mono, 3),
                        "stats": stats.snapshot(), "tcp": tcp_lifetime.snapshot(),
                        "raw": None if raw is None else raw.snapshot(),
                    })
                    next_heartbeat += args.heartbeat_s
                try:
                    payload = await asyncio.wait_for(ws.recv(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                received_ns = time.time_ns()
                received_mono = time.monotonic()
                received_mono_ns = time.monotonic_ns()
                if raw is not None:
                    if not raw.submit(
                        received_ns, payload, monotonic_ns=received_mono_ns,
                    ):
                        raise CaptureWriteError(
                            f"{spec.name} raw capture reached loss or byte cap"
                        )
                payload_bytes = len(payload.encode() if isinstance(payload, str) else payload)
                stats.observe(
                    parse_frame(spec.name, payload), payload_bytes,
                    received_ns, received_mono,
                )
        finally:
            connection_stop.set()
            tcp_task.cancel()
            await asyncio.gather(tcp_task, return_exceptions=True)


async def _source_loop(
    spec: SourceSpec, args: argparse.Namespace, sink: JsonlSink,
    raw: RawFrameWriter | None, stop_all: asyncio.Event,
) -> None:
    stats, tcp = SourceStats(), TcpExtrema()
    connection_id = reconnects = 0
    while not stop_all.is_set():
        connection_id += 1
        try:
            await _connection(
                spec, args, sink, raw, stop_all, stats, tcp, connection_id,
            )
        except asyncio.CancelledError:
            raise
        except CaptureWriteError:
            stop_all.set()
            raise
        except Exception as exc:
            reconnects += 1
            sink.emit({
                "kind": "source_closed", **_timestamp_fields(), "source": spec.name,
                "connection_id": connection_id, "reconnects": reconnects,
                "error_type": exc.__class__.__name__, "error": str(exc),
                "stats": stats.snapshot(), "tcp": tcp.snapshot(),
            })
            await asyncio.sleep(min(5.0, 0.1 * 2 ** min(reconnects - 1, 6)))
    sink.emit({
        "kind": "source_final", **_timestamp_fields(), "source": spec.name,
        "connections": connection_id, "reconnects": reconnects,
        "stats": stats.snapshot(), "tcp": tcp.snapshot(),
        "raw": None if raw is None else raw.snapshot(),
    })


async def run(args: argparse.Namespace) -> None:
    specs = source_specs(args.asset, args.sources.split(","))
    output = pathlib.Path(args.output)
    dataset = output.with_suffix(output.suffix + ".dataset.json")
    if dataset.exists() and not args.append:
        raise FileExistsError(f"refusing to overwrite {dataset}")
    sink = JsonlSink(output, append=args.append)
    started_at = time.time()
    clock_domain = clock_domain_identity()
    sink.emit({
        "kind": "capture_start", "schema": "project-fail-crossvenue-v1",
        **_timestamp_fields(), "clock_domain": clock_domain,
        "label": args.label, "asset": args.asset, "revision": args.revision,
        "sources": [dataclasses.asdict(spec) for spec in specs],
        "raw_limit_gb": args.raw_limit_gb, "raw_chunk_mb": args.raw_chunk_mb,
    })
    writers: dict[str, RawFrameWriter] = {}
    if args.raw_dir:
        for spec in specs:
            writers[spec.name] = RawFrameWriter(
                pathlib.Path(args.raw_dir), f"{args.label}-{spec.name}",
                limit_bytes=int(args.raw_limit_gb * 1024 ** 3),
                chunk_bytes=int(args.raw_chunk_mb * 1024 ** 2),
            )
    stop_all = asyncio.Event()
    timer = asyncio.get_running_loop().call_later(args.duration_s, stop_all.set)
    tasks = [asyncio.create_task(
        _source_loop(spec, args, sink, writers.get(spec.name), stop_all)
    ) for spec in specs]
    try:
        while not stop_all.is_set():
            if args.kill_file and pathlib.Path(args.kill_file).exists():
                sink.emit({
                    "kind": "stopped", **_timestamp_fields(), "reason": "kill_file",
                })
                stop_all.set()
                break
            await asyncio.sleep(0.5)
        await asyncio.gather(*tasks)
    finally:
        timer.cancel()
        stop_all.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for source, writer in writers.items():
            writer.close()
            sink.emit({
                "kind": "raw_final", **_timestamp_fields(), "source": source,
                "raw": writer.snapshot(),
            })
        sink.emit({
            "kind": "capture_end", **_timestamp_fields(),
            "clock_domain": clock_domain, "label": args.label,
        })
        sink.close()
        if writers:
            raw_manifests = []
            for source, writer in sorted(writers.items()):
                path = pathlib.Path(str(writer.manifest_path))
                raw_manifests.append({
                    "source": source,
                    "path": os.path.relpath(path, dataset.parent),
                    "sha256": _sha256(path),
                })
            manifest = {
                "schema": "project-fail-crossvenue-dataset-v1",
                "label": args.label, "asset": args.asset,
                "revision": args.revision, "started_at": started_at,
                "ended_at": time.time(),
                "clock_domain": clock_domain,
                "telemetry": {"path": os.path.relpath(output, dataset.parent),
                              "sha256": _sha256(output)},
                "raw_manifests": raw_manifests,
            }
            temporary = dataset.with_suffix(dataset.suffix + ".tmp")
            temporary.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, dataset)


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="btc", choices=("btc", "eth", "sol", "xrp"))
    parser.add_argument("--sources")
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--raw-dir")
    parser.add_argument("--duration-s", type=float, default=86_400)
    parser.add_argument("--heartbeat-s", type=float, default=20)
    parser.add_argument("--tcp-cadence-s", type=float, default=1)
    parser.add_argument("--max-queue", type=int, default=4096)
    parser.add_argument("--raw-limit-gb", type=float, default=20,
                        help="uncompressed input cap per source")
    parser.add_argument("--raw-chunk-mb", type=float, default=256)
    parser.add_argument("--kill-file", default="paper/CROSSVENUE_KILL")
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--revision", required=True, type=validated_revision)
    args = parser.parse_args(argv)
    if (not LABEL_RE.fullmatch(args.label) or args.duration_s <= 0
            or args.heartbeat_s <= 0 or args.tcp_cadence_s <= 0
            or args.max_queue <= 0 or args.raw_limit_gb <= 0
            or args.raw_chunk_mb <= 0):
        parser.error("invalid label, duration, queue, or raw byte limit")
    if args.raw_dir and args.append:
        parser.error("raw replay captures cannot append to an existing telemetry stream")
    requested_sources = args.sources or (
        "polymarket_rtds,binance_spot,binance_futures,deribit"
        if args.asset in {"btc", "eth"} else
        "polymarket_rtds,binance_spot,binance_futures"
    )
    args.sources = ",".join(
        value.strip() for value in requested_sources.split(",") if value.strip()
    )
    if not args.sources:
        parser.error("at least one source is required")
    try:
        source_specs(args.asset, args.sources.split(","))
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv: Sequence[str] | None = None) -> int:
    try:
        asyncio.run(run(arguments(argv)))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
