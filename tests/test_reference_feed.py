from __future__ import annotations

import asyncio
import json
import unittest

from paper.reference_feed import (
    INBOUND_LIVENESS_TIMEOUT_S,
    TOPIC,
    ReferenceFeed,
    ReferenceStall,
    parse_update,
    subscription_message,
)


class _SilentSocket:
    """Publisher that accepts the subscription then goes permanently silent.

    Reproduces Gen79: TCP stays open, PINGs are accepted, but no frame ever
    comes back. The old model looped on recv() timeouts forever.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.clock = 0.0

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        raise asyncio.TimeoutError


class ReferenceFeedTests(unittest.TestCase):
    def test_subscription_and_exact_update_parsing(self) -> None:
        message = json.loads(subscription_message(["eth", "btc", "btc"]))
        self.assertEqual(
            [subscription["filters"] for subscription in message["subscriptions"]],
            ['{"symbol":"btc/usd"}', '{"symbol":"eth/usd"}'],
        )

        raw = json.dumps({
            "topic": TOPIC,
            "payload": {
                "symbol": "btc/usd",
                "timestamp": "1700000000123",
                "window_s": "60",
                "full_accuracy_value": "65000123456789012345678",
            },
        })
        update = parse_update(raw, 1_700_000_001.5, {"btc"})

        self.assertIsNotNone(update)
        assert update is not None
        self.assertEqual(update.asset, "btc")
        self.assertEqual(update.observed_at, 1_700_000_000.123)
        self.assertEqual(update.value_e18, "65000123456789012345678")
        self.assertEqual(update.window_s, 60)

    def test_control_and_wrong_stream_messages_are_ignored(self) -> None:
        self.assertIsNone(parse_update("", 1, {"btc"}))
        self.assertIsNone(parse_update("PONG", 1, {"btc"}))
        self.assertIsNone(parse_update(json.dumps({
            "topic": TOPIC,
            "payload": {
                "symbol": "btc/usd", "timestamp": "1000", "window_s": 30,
                "full_accuracy_value": "1",
            },
        }), 1, {"btc"}))


class ReferenceLivenessTests(unittest.TestCase):
    """Gen79 regression: a silently dead publisher must force a reconnect.

    Drives the real ReferenceFeed.run() loop, not a copy of it.
    """

    def test_silent_publisher_forces_reconnect(self) -> None:
        import paper.reference_feed as module

        feed = ReferenceFeed(["btc"])
        sockets: list[_SilentSocket] = []

        class _Connect:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.socket = _SilentSocket()
                sockets.append(self.socket)

            async def __aenter__(self) -> "_SilentSocket":
                if len(sockets) > 1:
                    # second connection attempt proves the first was abandoned
                    raise asyncio.CancelledError
                return self.socket

            async def __aexit__(self, *exc: object) -> bool:
                return False

        clock = {"now": 1_000.0}

        def fake_monotonic() -> float:
            clock["now"] += 1.0
            return clock["now"]

        original_connect = module.websockets.connect
        original_monotonic = module.time.monotonic
        original_sleep = module.asyncio.sleep
        module.websockets.connect = _Connect
        module.time.monotonic = fake_monotonic

        async def no_sleep(_seconds: float) -> None:
            return None

        module.asyncio.sleep = no_sleep
        try:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(feed.run(lambda update: None))
        finally:
            module.websockets.connect = original_connect
            module.time.monotonic = original_monotonic
            module.asyncio.sleep = original_sleep

        self.assertEqual(feed.stalls, 1, "silent stream must be detected once")
        self.assertEqual(feed.reconnects, 1, "stall must trigger a reconnect")
        self.assertEqual(feed.snapshot()["stalls"], 1)
        self.assertIn("PING", sockets[0].sent)
        self.assertEqual(len(sockets), 2, "feed must attempt a fresh connection")

    def test_healthy_traffic_renews_the_lease(self) -> None:
        """Any inbound frame - even an unparsed PONG - must renew liveness."""
        import paper.reference_feed as module

        feed = ReferenceFeed(["btc"])
        frames = ["PONG"] * 40

        class _TalkingSocket:
            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send(self, message: str) -> None:
                self.sent.append(message)

            async def recv(self) -> str:
                if not frames:
                    raise asyncio.CancelledError
                return frames.pop()

        class _Connect:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.socket = _TalkingSocket()

            async def __aenter__(self) -> "_TalkingSocket":
                return self.socket

            async def __aexit__(self, *exc: object) -> bool:
                return False

        clock = {"now": 1_000.0}

        def fake_monotonic() -> float:
            clock["now"] += 1.0
            return clock["now"]

        original_connect = module.websockets.connect
        original_monotonic = module.time.monotonic
        module.websockets.connect = _Connect
        module.time.monotonic = fake_monotonic
        try:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(feed.run(lambda update: None))
        finally:
            module.websockets.connect = original_connect
            module.time.monotonic = original_monotonic

        self.assertEqual(feed.stalls, 0, "steady inbound frames must not stall")


if __name__ == "__main__":
    unittest.main()
