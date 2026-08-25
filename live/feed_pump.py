"""Drain WebSocket frames independently from ordered event processing."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Protocol

EVENT_QUEUE_CAPACITY = 8192
PROCESS_YIELD_EVERY = 64


class WebSocketLike(Protocol):
    async def send(self, message: str) -> object: ...
    async def recv(self) -> str | bytes: ...


class FeedBacklogError(RuntimeError):
    pass


class FeedPump:
    """Keep socket reads responsive while preserving handler causality."""

    def __init__(
        self, handler: Callable[[dict[str, object]], None],
        capacity: int = EVENT_QUEUE_CAPACITY,
    ) -> None:
        self.handler = handler
        self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(capacity)
        self.high_water = 0

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
                    self.queue.put_nowait(event)
                except asyncio.QueueFull as exc:
                    raise FeedBacklogError(
                        f"market event queue reached {self.queue.maxsize}",
                    ) from exc
                self.high_water = max(self.high_water, self.queue.qsize())

    async def _process(self, stop: asyncio.Event) -> None:
        processed = 0
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
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
