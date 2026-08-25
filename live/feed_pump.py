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


class FeedPumpStats:
    """Connection-independent queue telemetry for a runner generation."""

    def __init__(self) -> None:
        self.high_water = 0
        self.interval_high_water = 0
        self.residence_max_ms = 0.0
        self.interval_residence_max_ms = 0.0

    def observe_depth(self, depth: int) -> None:
        self.high_water = max(self.high_water, depth)
        self.interval_high_water = max(self.interval_high_water, depth)

    def observe_residence(self, residence_ms: float) -> None:
        self.residence_max_ms = max(self.residence_max_ms, residence_ms)
        self.interval_residence_max_ms = max(
            self.interval_residence_max_ms, residence_ms,
        )

    def snapshot(self, *, reset_interval: bool = False) -> dict[str, int]:
        snapshot = {
            "hwm": self.high_water,
            "interval_hwm": self.interval_high_water,
            "residence_max_ms": round(self.residence_max_ms),
            "interval_residence_max_ms": round(self.interval_residence_max_ms),
        }
        if reset_interval:
            self.interval_high_water = 0
            self.interval_residence_max_ms = 0.0
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
    ) -> None:
        self.handler = handler
        self.queue: asyncio.Queue[tuple[dict[str, object], float]] = asyncio.Queue(capacity)
        self.stats = stats or FeedPumpStats()

    @property
    def high_water(self) -> int:
        return self.stats.high_water

    async def _read(self, ws: WebSocketLike, stop: asyncio.Event) -> None:
        last_ping = time.monotonic()
        while not stop.is_set():
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
            if raw == "PONG":
                continue
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            for event in payload if isinstance(payload, list) else [payload]:
                if not isinstance(event, dict):
                    continue
                try:
                    self.queue.put_nowait((event, time.monotonic()))
                except asyncio.QueueFull as exc:
                    raise FeedBacklogError(
                        f"market event queue reached {self.queue.maxsize}",
                    ) from exc
                self.stats.observe_depth(self.queue.qsize())

    async def _process(self, stop: asyncio.Event) -> None:
        processed = 0
        while not stop.is_set():
            try:
                event, enqueued_at = await asyncio.wait_for(
                    self.queue.get(), timeout=0.5,
                )
            except asyncio.TimeoutError:
                continue
            residence_ms = max(0.0, (time.monotonic() - enqueued_at) * 1000)
            self.stats.observe_residence(residence_ms)
            self.handler(event)
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

    def snapshot(self, *, reset_interval: bool = False) -> dict[str, int]:
        return self.stats.snapshot(reset_interval=reset_interval)
