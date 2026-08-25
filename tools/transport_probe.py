#!/usr/bin/env python3
"""Read-only rotating market-WebSocket transport attribution probe."""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
from collections.abc import Sequence

import websockets

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from live.feed_health import MAX_FUTURE_EVENT_SKEW_S, event_time_s
from live.feed_pump import WebSocketLike, subscription_messages, websocket_frame_depth
from live.window_clock import boundary_aligned_delay
from paper.market_metadata import ActiveMarket, fetch_active_market
from tools.market_windows import ASSET_PREFIX
from tools.transport_telemetry import (
    CaptureWriteError,
    FrameCounters,
    JsonlSink,
    RawFrameWriter,
    TcpExtrema,
    websocket_tcp_info,
)


MKT_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _payload_events(
    raw: str | bytes, received_at: float,
) -> tuple[int, int, float, float | None, int, int, bool]:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0, 0, 0.0, None, 0, 0, True
    events = ([event for event in payload if isinstance(event, dict)]
              if isinstance(payload, list) else
              [payload] if isinstance(payload, dict) else [])
    stale = 0
    max_age = 0.0
    min_age: float | None = None
    missing = negative = 0
    for event in events:
        source_at = event_time_s(event)
        if source_at is None:
            missing += 1
            continue
        age_ms = (received_at - source_at) * 1000
        min_age = age_ms if min_age is None else min(min_age, age_ms)
        negative += int(age_ms < 0)
        max_age = max(max_age, age_ms)
        stale += int(
            age_ms > 400 or age_ms < -MAX_FUTURE_EVENT_SKEW_S * 1000
        )
    return len(events), stale, max_age, min_age, missing, negative, False


async def _market(asset: str, base: int) -> ActiveMarket | None:
    return await asyncio.to_thread(fetch_active_market, asset, base, 1)


def _market_fields(market: ActiveMarket) -> dict[str, object]:
    return {
        "asset": market.asset,
        "slug": market.slug,
        "start": market.start,
        "condition_id": market.condition_id,
        "up_token": market.up_token,
        "down_token": market.down_token,
        "min_order_size": market.min_order_size,
    }


async def _tcp_sampler(
    ws: object, stop: asyncio.Event, lifetime: TcpExtrema, interval: TcpExtrema,
    cadence_s: float,
) -> None:
    while not stop.is_set():
        sample = websocket_tcp_info(ws)
        lifetime.observe(sample)
        interval.observe(sample)
        await asyncio.sleep(cadence_s)


async def _rotate(
    ws: WebSocketLike, send_lock: asyncio.Lock, stop: asyncio.Event, asset: str,
    subscribed: set[str], market: ActiveMarket, sink: JsonlSink, label: str,
) -> None:
    current_market = market
    while not stop.is_set():
        base = int(time.time() // 300) * 300
        wanted_slug = f"{ASSET_PREFIX[asset]}-{base}"
        if wanted_slug != current_market.slug:
            try:
                result = await _market(asset, base)
            except RuntimeError as exc:
                sink.emit({"kind": "discovery_error", "ts": time.time(),
                           "label": label, "error": str(exc)})
                result = None
            if result is not None:
                current_market = result
                target = {result.up_token, result.down_token}
                messages = subscription_messages(subscribed, target)
                async with send_lock:
                    for message in messages:
                        await ws.send(json.dumps(message))
                sink.emit({
                    "kind": "rotation", "ts": time.time(), "label": label,
                    **_market_fields(result), "added_first": bool(messages),
                    "messages": messages,
                })
                subscribed.clear()
                subscribed.update(target)
        await asyncio.sleep(boundary_aligned_delay(time.time()))


async def _connection(
    args: argparse.Namespace, sink: JsonlSink, raw_writer: RawFrameWriter | None,
    connection_id: int, global_stop: asyncio.Event,
) -> None:
    base = int(time.time() // 300) * 300
    result = await _market(args.asset, base)
    if result is None:
        raise RuntimeError(f"active {args.asset} market unavailable")
    market = result
    tokens = {market.up_token, market.down_token}
    connected_at = time.monotonic()
    async with websockets.connect(
        MKT_WS, ping_interval=None, open_timeout=12, close_timeout=0.1,
        max_queue=args.max_queue,
    ) as ws:
        send_lock = asyncio.Lock()
        async with send_lock:
            await ws.send(json.dumps({"assets_ids": sorted(tokens), "type": "market"}))
        sink.emit({
            "kind": "connected", "ts": time.time(), "label": args.label,
            "connection_id": connection_id, **_market_fields(market),
        })
        stop = asyncio.Event()
        lifetime, interval = FrameCounters(), FrameCounters()
        tcp_lifetime, tcp_interval = TcpExtrema(), TcpExtrema()
        rotate = asyncio.create_task(
            _rotate(ws, send_lock, stop, args.asset, tokens, market, sink, args.label)
        )
        tcp = asyncio.create_task(
            _tcp_sampler(ws, stop, tcp_lifetime, tcp_interval, args.tcp_cadence_s)
        )
        heartbeat_index = 0
        next_heartbeat = time.monotonic() + args.heartbeat_s
        last_ping = time.monotonic()
        try:
            while not global_stop.is_set():
                if args.kill_file and pathlib.Path(args.kill_file).exists():
                    global_stop.set()
                    continue
                now_mono = time.monotonic()
                if now_mono >= next_heartbeat:
                    heartbeat_index += 1
                    sink.emit({
                        "kind": "heartbeat", "heartbeat_index": heartbeat_index,
                        "ts": time.time(), "label": args.label,
                        "connection_id": connection_id,
                        "connected_s": round(now_mono - connected_at, 3),
                        "subscribed": len(tokens),
                        "ws_depth": websocket_frame_depth(ws),
                        "interval": interval.snapshot(reset=True),
                        "lifetime": lifetime.snapshot(),
                        "tcp_interval": tcp_interval.snapshot(reset=True),
                        "tcp_lifetime": tcp_lifetime.snapshot(),
                        "raw": None if raw_writer is None else raw_writer.snapshot(),
                    })
                    next_heartbeat += args.heartbeat_s
                if now_mono - last_ping >= 10:
                    async with send_lock:
                        await ws.send("PING")
                    last_ping = now_mono
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                received_mono, received_at = time.monotonic(), time.time()
                received_ns = time.time_ns()
                received_mono_ns = time.monotonic_ns()
                for counters in (lifetime, interval):
                    counters.observe_gap(received_mono)
                    counters.last_frame_wall = received_at
                if raw == "PONG":
                    lifetime.pongs += 1
                    interval.pongs += 1
                    continue
                if raw_writer is not None:
                    if not raw_writer.submit(
                        received_ns, raw, monotonic_ns=received_mono_ns,
                    ):
                        raise CaptureWriteError(
                            "transport raw capture reached loss or byte cap"
                        )
                payload_bytes = len(raw.encode() if isinstance(raw, str) else raw)
                (
                    events, stale, max_age, min_age, missing,
                    negative, parse_error,
                ) = _payload_events(raw, received_at)
                for counters in (lifetime, interval):
                    counters.frames += 1
                    counters.payload_bytes += payload_bytes
                    counters.events += events
                    counters.parse_errors += int(parse_error)
                    counters.missing_source_time += missing
                    counters.negative_source_age += negative
                    counters.stale_events += stale
                    if min_age is not None:
                        counters.min_source_age_ms = (
                            min_age if counters.min_source_age_ms is None else
                            min(counters.min_source_age_ms, min_age)
                        )
                    counters.max_source_age_ms = max(
                        counters.max_source_age_ms, max_age,
                    )
        finally:
            sink.emit({
                "kind": "connection_end", "ts": time.time(), "label": args.label,
                "connection_id": connection_id,
                "connected_s": round(time.monotonic() - connected_at, 3),
                "last_frame_age_ms": (None if lifetime.last_frame_mono is None else
                                      round((time.monotonic() - lifetime.last_frame_mono)
                                            * 1000, 3)),
                "lifetime": lifetime.snapshot(),
                "tcp_lifetime": tcp_lifetime.snapshot(),
                "raw": None if raw_writer is None else raw_writer.snapshot(),
            })
            stop.set()
            for task in (rotate, tcp):
                task.cancel()
            await asyncio.gather(rotate, tcp, return_exceptions=True)


async def run(args: argparse.Namespace) -> None:
    sink = JsonlSink(pathlib.Path(args.output), append=args.append)
    raw_writer = None
    if args.raw_dir:
        raw_writer = RawFrameWriter(
            pathlib.Path(args.raw_dir), args.label,
            limit_bytes=int(args.raw_limit_gb * 1024 ** 3),
            chunk_bytes=int(args.raw_chunk_mb * 1024 ** 2),
        )
    global_stop = asyncio.Event()
    timer = asyncio.get_running_loop().call_later(args.duration_s, global_stop.set)
    started = time.monotonic()
    connection_id = reconnects = 0
    try:
        while time.monotonic() - started < args.duration_s:
            if args.kill_file and pathlib.Path(args.kill_file).exists():
                sink.emit({"kind": "stopped", "ts": time.time(), "label": args.label,
                           "reason": "kill_file"})
                break
            connection_id += 1
            try:
                await _connection(args, sink, raw_writer, connection_id, global_stop)
            except Exception as exc:
                if isinstance(exc, CaptureWriteError):
                    raise
                reconnects += 1
                sink.emit({"kind": "closed", "ts": time.time(), "label": args.label,
                           "connection_id": connection_id, "reconnects": reconnects,
                           "error_type": exc.__class__.__name__, "error": str(exc)})
                await asyncio.sleep(min(2.0, 0.1 * 2 ** min(reconnects - 1, 5)))
    finally:
        timer.cancel()
        global_stop.set()
        if raw_writer is not None:
            raw_writer.close()
            sink.emit({"kind": "raw_final", "ts": time.time(), "label": args.label,
                       "raw": raw_writer.snapshot()})
        sink.close()


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default="btc", choices=("btc", "eth", "sol", "xrp"))
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--duration-s", type=float, default=86_400)
    parser.add_argument("--heartbeat-s", type=float, default=20)
    parser.add_argument("--tcp-cadence-s", type=float, default=0.1)
    parser.add_argument("--max-queue", type=int, default=1024)
    parser.add_argument("--kill-file", default="paper/TRANSPORT_KILL")
    parser.add_argument("--raw-dir")
    parser.add_argument("--raw-limit-gb", type=float, default=2)
    parser.add_argument("--raw-chunk-mb", type=float, default=256)
    parser.add_argument("--append", action="store_true")
    args = parser.parse_args(argv)
    if (args.duration_s <= 0 or args.heartbeat_s <= 0 or args.tcp_cadence_s <= 0
            or args.max_queue <= 0 or args.raw_limit_gb <= 0
            or args.raw_chunk_mb <= 0):
        parser.error("durations, queue sizes, and raw limits must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
