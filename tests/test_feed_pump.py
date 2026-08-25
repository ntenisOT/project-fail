import asyncio
import json
import time

from live.feed_pump import (
    FeedPump,
    FeedPumpStats,
    planned_boundary_refresh_allowed,
    subscription_messages,
    subscription_transition,
)
from live.loop_health import EventLoopHealth


def test_feed_pump_drains_socket_before_ordered_processing_finishes() -> None:
    seen: list[int] = []
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
    pump = FeedPump(handle, capacity=128, stats=stats)
    asyncio.run(pump.run(Socket(), stop))

    assert seen == list(range(100))
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


def test_subscription_rotation_adds_before_removing() -> None:
    assert subscription_messages({"old-up", "old-down"}, {"new-up", "new-down"}) == [
        {"operation": "subscribe", "assets_ids": ["new-down", "new-up"]},
        {"operation": "unsubscribe", "assets_ids": ["old-down", "old-up"]},
    ]


def test_planned_refresh_requires_a_new_uncommitted_window() -> None:
    assert planned_boundary_refresh_allowed(301.0, [300.0], has_commitment=False)
    assert not planned_boundary_refresh_allowed(301.0, [], has_commitment=False)
    assert not planned_boundary_refresh_allowed(301.0, [300.0], has_commitment=True)
    assert not planned_boundary_refresh_allowed(311.0, [300.0], has_commitment=False)
    assert not planned_boundary_refresh_allowed(299.0, [300.0], has_commitment=False)


def test_subscription_transition_preserves_committed_exposure() -> None:
    planned, messages = subscription_transition(
        {"old-up", "old-down"}, {"new-up", "new-down"}, now=301.0,
        window_starts=[300.0], has_commitment=False,
    )
    assert planned and messages == []

    planned, messages = subscription_transition(
        {"old-up", "old-down"}, {"new-up", "new-down"}, now=301.0,
        window_starts=[300.0], has_commitment=True,
    )
    assert not planned
    assert messages == subscription_messages(
        {"old-up", "old-down"}, {"new-up", "new-down"},
    )


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
