"""Drain WebSocket frames independently from ordered event processing."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Protocol, Sequence

EVENT_QUEUE_CAPACITY = 8192
PROCESS_YIELD_EVERY = 64


class WebSocketLike(Protocol):
    async def send(self, message: str) -> object: ...
    async def recv(self) -> str | bytes: ...


class FeedBacklogError(RuntimeError):
    pass


def websocket_frame_depth(ws: object) -> int | None:
    """Read the websockets client's internal frame-buffer depth when exposed."""
    receiver = getattr(ws, "recv_messages", None)
    frames = getattr(receiver, "frames", None)
    if frames is None:
        return None
    try:
        return len(frames)
    except TypeError:
        return None


class FeedPumpStats:
    """Connection-independent queue telemetry for a runner generation."""

    def __init__(self) -> None:
        self.high_water = 0
        self.interval_high_water = 0
        self.residence_max_ms = 0.0
        self.interval_residence_max_ms = 0.0
        self.frames = 0
        self.interval_frames = 0
        self.frame_events = 0
        self.interval_frame_events = 0
        self.frame_event_max = 0
        self.parse_max_ms = 0.0
        self.interval_parse_max_ms = 0.0
        self.ws_frame_high_water = 0
        self.interval_ws_frame_high_water = 0
        self.ws_frame_samples = 0
        self.interval_ws_frame_samples = 0
        self.ws_depth_unavailable = 0
        self.interval_ws_depth_unavailable = 0

    def observe_depth(self, depth: int) -> None:
        self.high_water = max(self.high_water, depth)
        self.interval_high_water = max(self.interval_high_water, depth)

    def observe_residence(self, residence_ms: float) -> None:
        self.residence_max_ms = max(self.residence_max_ms, residence_ms)
        self.interval_residence_max_ms = max(
            self.interval_residence_max_ms, residence_ms,
        )

    def observe_frame(self, events: int, parse_ms: float) -> None:
        self.frames += 1
        self.interval_frames += 1
        self.frame_events += events
        self.interval_frame_events += events
        self.frame_event_max = max(self.frame_event_max, events)
        self.parse_max_ms = max(self.parse_max_ms, parse_ms)
        self.interval_parse_max_ms = max(self.interval_parse_max_ms, parse_ms)

    def observe_ws_depth(self, depth: int | None) -> None:
        if depth is None:
            self.ws_depth_unavailable += 1
            self.interval_ws_depth_unavailable += 1
            return
        self.ws_frame_samples += 1
        self.interval_ws_frame_samples += 1
        self.ws_frame_high_water = max(self.ws_frame_high_water, depth)
        self.interval_ws_frame_high_water = max(
            self.interval_ws_frame_high_water, depth,
        )

    def snapshot(self, *, reset_interval: bool = False) -> dict[str, int | float]:
        snapshot = {
            "hwm": self.high_water,
            "interval_hwm": self.interval_high_water,
            "residence_max_ms": round(self.residence_max_ms),
            "interval_residence_max_ms": round(self.interval_residence_max_ms),
            "frames": self.frames,
            "interval_frames": self.interval_frames,
            "frame_events": self.frame_events,
            "interval_frame_events": self.interval_frame_events,
            "frame_event_max": self.frame_event_max,
            "parse_max_ms": round(self.parse_max_ms, 3),
            "interval_parse_max_ms": round(self.interval_parse_max_ms, 3),
            "ws_hwm": self.ws_frame_high_water,
            "interval_ws_hwm": self.interval_ws_frame_high_water,
            "ws_samples": self.ws_frame_samples,
            "interval_ws_samples": self.interval_ws_frame_samples,
            "ws_unavailable": self.ws_depth_unavailable,
            "interval_ws_unavailable": self.interval_ws_depth_unavailable,
        }
        if reset_interval:
            self.interval_high_water = 0
            self.interval_residence_max_ms = 0.0
            self.interval_frames = 0
            self.interval_frame_events = 0
            self.interval_parse_max_ms = 0.0
            self.interval_ws_frame_high_water = 0
            self.interval_ws_frame_samples = 0
            self.interval_ws_depth_unavailable = 0
        return snapshot


def subscription_messages(
    current: set[str], target: set[str],
) -> list[dict[str, str | Sequence[str]]]:
    """Rotate market tokens without dropping the socket or snapshot coverage."""
    messages: list[dict[str, str | Sequence[str]]] = []
    added, removed = sorted(target - current), sorted(current - target)
    if added:
        messages.append({"operation": "subscribe", "assets_ids": added})
    if removed:
        messages.append({"operation": "unsubscribe", "assets_ids": removed})
    return messages


class FeedPump:
    """Keep socket reads responsive while preserving handler causality."""

    def __init__(
        self, handler: Callable[[dict[str, object]], None],
        capacity: int = EVENT_QUEUE_CAPACITY,
        stats: FeedPumpStats | None = None,
        frame_sink: Callable[[int, int, str | bytes], int | None] | None = None,
        processed_sink: Callable[[int, int, int, int], None] | None = None,
        timestamped_handler: Callable[[dict[str, object], int, int], None] | None = None,
    ) -> None:
        self.handler = handler
        self.queue: asyncio.Queue[tuple[dict[str, object], float, int, int]] = (
            asyncio.Queue(capacity)
        )
        self.stats = stats or FeedPumpStats()
        self.frame_sink = frame_sink
        self.processed_sink = processed_sink
        self.timestamped_handler = timestamped_handler
        self._next_frame_id = 0

    @property
    def high_water(self) -> int:
        return self.stats.high_water

    async def _read(self, ws: WebSocketLike, stop: asyncio.Event) -> None:
        last_ping = time.monotonic()
        while not stop.is_set():
            self.stats.observe_ws_depth(websocket_frame_depth(ws))
            elapsed = time.monotonic() - last_ping
            if elapsed >= 10:
                await ws.send("PING")
                last_ping = time.monotonic()
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=min(0.5, max(0.1, 10 - elapsed)),
                )
            except asyncio.TimeoutError:
                continue
            self.stats.observe_ws_depth(websocket_frame_depth(ws))
            if raw == "PONG":
                continue
            frame_id = self._next_frame_id
            self._next_frame_id += 1
            if self.frame_sink is not None:
                captured_id = self.frame_sink(time.time_ns(), time.monotonic_ns(), raw)
                if captured_id is not None:
                    frame_id = captured_id
            parse_started = time.monotonic()
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            events = [
                event for event in payload if isinstance(event, dict)
            ] if isinstance(payload, list) else (
                [payload] if isinstance(payload, dict) else []
            )
            self.stats.observe_frame(
                len(events), (time.monotonic() - parse_started) * 1000,
            )
            for event_index, event in enumerate(events):
                try:
                    self.queue.put_nowait(
                        (event, time.monotonic(), frame_id, event_index),
                    )
                except asyncio.QueueFull as exc:
                    raise FeedBacklogError(
                        f"market event queue reached {self.queue.maxsize}",
                    ) from exc
                self.stats.observe_depth(self.queue.qsize())

    async def _process(self, stop: asyncio.Event) -> None:
        processed = 0
        while not stop.is_set():
            try:
                event, enqueued_at, frame_id, event_index = await asyncio.wait_for(
                    self.queue.get(), timeout=0.5,
                )
            except asyncio.TimeoutError:
                continue
            residence_ms = max(0.0, (time.monotonic() - enqueued_at) * 1000)
            self.stats.observe_residence(residence_ms)
            if self.timestamped_handler is None and self.processed_sink is None:
                self.handler(event)
            else:
                handler_wall_ns = time.time_ns()
                handler_monotonic_ns = time.monotonic_ns()
                if self.timestamped_handler is None:
                    self.handler(event)
                else:
                    self.timestamped_handler(
                        event, handler_wall_ns, handler_monotonic_ns,
                    )
                if self.processed_sink is not None:
                    self.processed_sink(
                        handler_wall_ns, handler_monotonic_ns, frame_id, event_index,
                    )
            processed += 1
            if processed % PROCESS_YIELD_EVERY == 0:
                await asyncio.sleep(0)

    async def run(self, ws: WebSocketLike, stop: asyncio.Event) -> None:
        reader = asyncio.create_task(self._read(ws, stop))
        processor = asyncio.create_task(self._process(stop))
        tasks = {reader, processor}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def snapshot(self, *, reset_interval: bool = False) -> dict[str, int | float]:
        return self.stats.snapshot(reset_interval=reset_interval)
