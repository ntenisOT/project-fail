import asyncio
import json
import time

import pytest

from live.feed_pump import (
    FeedBacklogError,
    FeedIntegrityError,
    FeedPump,
    FeedPumpStats,
    subscription_messages,
    websocket_frame_depth,
)
from live.loop_health import EventLoopHealth


def test_feed_pump_drains_socket_before_ordered_processing_finishes() -> None:
    seen: list[int] = []
    raw_frames: list[tuple[int, int, str | bytes]] = []
    stop = asyncio.Event()

    class Socket:
        def __init__(self) -> None:
            self.sent = False

        async def send(self, message: str) -> None:
            del message

        async def recv(self) -> str:
            if not self.sent:
                self.sent = True
                return json.dumps([{"sequence": value} for value in range(100)])
            while len(seen) < 100:
                await asyncio.sleep(0)
            stop.set()
            return "PONG"

    def handle(event: dict[str, object]) -> None:
        if not seen:
            time.sleep(0.002)
        seen.append(int(str(event["sequence"])))

    stats = FeedPumpStats()
    pump = FeedPump(
        handle, capacity=128, stats=stats,
        frame_sink=lambda wall, mono, raw: raw_frames.append((wall, mono, raw)),
    )
    asyncio.run(pump.run(Socket(), stop))

    assert seen == list(range(100))
    assert len(raw_frames) == 1
    assert raw_frames[0][0] > 0 and raw_frames[0][1] > 0
    assert pump.high_water == 100
    snapshot = pump.snapshot(reset_interval=True)
    assert snapshot["hwm"] == snapshot["interval_hwm"] == 100
    assert snapshot["residence_max_ms"] > 0
    assert snapshot["frames"] >= 1
    assert snapshot["frame_events"] == 100
    assert snapshot["frame_event_max"] == 100
    assert snapshot["parse_max_ms"] >= 0
    after = pump.snapshot()
    assert after["hwm"] == snapshot["hwm"]
    assert after["residence_max_ms"] == snapshot["residence_max_ms"]
    assert after["interval_hwm"] == after["interval_residence_max_ms"] == 0
    assert after["interval_frames"] == after["interval_frame_events"] == 0
    assert after["interval_parse_max_ms"] == 0
    replacement = FeedPump(handle, capacity=128, stats=stats)
    assert replacement.snapshot()["hwm"] == 100


def test_feed_pump_acknowledges_only_the_processed_prefix_on_tail_loss() -> None:
    seen: list[int] = []
    processed: list[tuple[int, int]] = []
    raw_frames: list[str | bytes] = []

    class Socket:
        async def send(self, message: str) -> None:
            del message

        async def recv(self) -> str:
            return json.dumps([{"sequence": value} for value in range(20)])

    def frame_sink(wall_ns: int, monotonic_ns: int, raw: str | bytes) -> int:
        assert wall_ns > 0 and monotonic_ns > 0
        raw_frames.append(raw)
        return 41

    pump = FeedPump(
        lambda event: seen.append(int(str(event["sequence"]))),
        capacity=8,
        frame_sink=frame_sink,
        processed_sink=lambda _wall, _mono, frame, event: processed.append(
            (frame, event)
        ),
    )

    with pytest.raises(FeedBacklogError):
        asyncio.run(pump.run(Socket(), asyncio.Event()))

    assert len(raw_frames) == 1
    assert 0 <= len(seen) < 20
    assert processed == [(41, index) for index in range(len(seen))]


def test_feed_pump_reconnects_instead_of_hiding_malformed_frames() -> None:
    class Socket:
        def __init__(self, raw: str) -> None:
            self.raw = raw

        async def send(self, message: str) -> None:
            del message

        async def recv(self) -> str:
            return self.raw

    for raw in ("not-json", json.dumps([{"ok": True}, 7]), json.dumps("scalar")):
        with pytest.raises(FeedIntegrityError):
            asyncio.run(FeedPump(lambda _event: None).run(Socket(raw), asyncio.Event()))


def test_feed_pump_fails_closed_when_ping_receives_no_inbound_liveness() -> None:
    class Socket:
        sent: list[str] = []

        async def send(self, message: str) -> None:
            self.sent.append(message)

        async def recv(self) -> str:
            await asyncio.sleep(1)
            return "never reached"

    socket = Socket()
    pump = FeedPump(
        lambda _event: None, ping_interval_s=0.005, inbound_liveness_timeout_s=0.01,
    )

    with pytest.raises(FeedIntegrityError, match="inbound liveness timeout"):
        asyncio.run(pump.run(socket, asyncio.Event()))
    assert socket.sent and set(socket.sent) == {"PING"}


def test_subscription_rotation_adds_before_removing() -> None:
    assert subscription_messages({"old-up", "old-down"}, {"new-up", "new-down"}) == [
        {"operation": "subscribe", "assets_ids": ["new-down", "new-up"]},
        {"operation": "unsubscribe", "assets_ids": ["old-down", "old-up"]},
    ]


def test_websocket_internal_frame_depth_is_reported_and_reset() -> None:
    class Receiver:
        frames = [object()] * 17

    class Socket:
        recv_messages = Receiver()

    stats = FeedPumpStats()
    depth = websocket_frame_depth(Socket())
    assert depth == 17
    stats.observe_ws_depth(depth)

    snapshot = stats.snapshot(reset_interval=True)
    assert snapshot["ws_hwm"] == snapshot["interval_ws_hwm"] == 17
    assert snapshot["ws_samples"] == snapshot["interval_ws_samples"] == 1
    assert snapshot["ws_unavailable"] == 0
    after = stats.snapshot()
    assert after["ws_hwm"] == 17
    assert after["interval_ws_hwm"] == after["interval_ws_samples"] == 0


def test_event_loop_health_retains_lifetime_and_resets_interval_peak() -> None:
    health = EventLoopHealth()
    health.observe(2.4)
    health.observe(8.6)

    snapshot = health.snapshot(reset_interval=True)

    assert snapshot == {
        "p50_ms": 2, "p90_ms": 2, "max_ms": 9,
        "interval_max_ms": 9, "lifetime_max_ms": 9,
    }
    assert health.snapshot()["interval_max_ms"] == 0
    assert health.snapshot()["lifetime_max_ms"] == 9
