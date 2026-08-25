import asyncio
import json

from live.feed_pump import FeedPump, subscription_messages


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
        seen.append(int(str(event["sequence"])))

    pump = FeedPump(handle, capacity=128)
    asyncio.run(pump.run(Socket(), stop))

    assert seen == list(range(100))
    assert pump.high_water == 100


def test_subscription_rotation_adds_before_removing() -> None:
    assert subscription_messages({"old-up", "old-down"}, {"new-up", "new-down"}) == [
        {"operation": "subscribe", "assets_ids": ["new-down", "new-up"]},
        {"operation": "unsubscribe", "assets_ids": ["old-down", "old-up"]},
    ]
