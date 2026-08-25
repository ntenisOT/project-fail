import asyncio
import json
import time

from live.feed_pump import FeedPump, FeedPumpStats, subscription_messages


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
    after = pump.snapshot()
    assert after["hwm"] == snapshot["hwm"]
    assert after["residence_max_ms"] == snapshot["residence_max_ms"]
    assert after["interval_hwm"] == after["interval_residence_max_ms"] == 0
    replacement = FeedPump(handle, capacity=128, stats=stats)
    assert replacement.snapshot()["hwm"] == 100


def test_subscription_rotation_adds_before_removing() -> None:
    assert subscription_messages({"old-up", "old-down"}, {"new-up", "new-down"}) == [
        {"operation": "subscribe", "assets_ids": ["new-down", "new-up"]},
        {"operation": "unsubscribe", "assets_ids": ["old-down", "old-up"]},
    ]
