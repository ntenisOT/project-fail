from __future__ import annotations

import json
import unittest

from paper.reference_feed import TOPIC, parse_update, subscription_message


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


if __name__ == "__main__":
    unittest.main()
