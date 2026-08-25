"""Official Chainlink 60-second TWAP shadow stream for crypto markets."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from collections.abc import Callable, Iterable

import websockets


RTDS_WS = "wss://ws-live-data.polymarket.com"
TOPIC = "crypto_prices_twap_sixty"
log = logging.getLogger("paper.reference")


@dataclasses.dataclass(frozen=True)
class ReferenceUpdate:
    asset: str
    observed_at: float
    received_at: float
    value_e18: str
    window_s: int


def subscription_message(assets: Iterable[str]) -> str:
    subscriptions = [
        {
            "topic": TOPIC,
            "type": "update",
            "filters": json.dumps(
                {"symbol": f"{asset.lower()}/usd"}, separators=(",", ":"),
            ),
        }
        for asset in sorted(set(assets))
    ]
    return json.dumps({"action": "subscribe", "subscriptions": subscriptions})


def parse_update(raw: str | bytes, received_at: float,
                 assets: set[str]) -> ReferenceUpdate | None:
    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(message, dict) or message.get("topic") != TOPIC:
        return None
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None
    symbol = str(payload.get("symbol") or "").lower()
    asset = symbol.removesuffix("/usd")
    try:
        observed_at = int(str(payload["timestamp"])) / 1000
        window_s = int(str(payload["window_s"]))
        value_e18 = str(int(str(payload["full_accuracy_value"])))
    except (KeyError, TypeError, ValueError):
        return None
    if asset not in assets or symbol != f"{asset}/usd" or window_s != 60:
        return None
    return ReferenceUpdate(asset, observed_at, received_at, value_e18, window_s)


class ReferenceFeed:
    def __init__(self, assets: Iterable[str]) -> None:
        self.assets = {asset.lower() for asset in assets}
        self.updates = 0
        self.reconnects = 0
        self.max_age_ms = 0.0

    def snapshot(self) -> dict[str, int]:
        return {
            "updates": self.updates,
            "reconnects": self.reconnects,
            "max_age_ms": round(self.max_age_ms),
        }

    async def run(self, on_update: Callable[[ReferenceUpdate], None]) -> None:
        retry_s = 0.1
        while True:
            connected_at: float | None = None
            try:
                async with websockets.connect(
                    RTDS_WS, ping_interval=None, open_timeout=12, close_timeout=0.1,
                ) as ws:
                    connected_at = time.monotonic()
                    await ws.send(subscription_message(self.assets))
                    next_ping = time.monotonic() + 5
                    while True:
                        if time.monotonic() >= next_ping:
                            await ws.send("PING")
                            next_ping = time.monotonic() + 5
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        received_at = time.time()
                        update = parse_update(raw, received_at, self.assets)
                        if update is None:
                            continue
                        self.updates += 1
                        self.max_age_ms = max(
                            self.max_age_ms,
                            max(0.0, 1000 * (received_at - update.observed_at)),
                        )
                        on_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if connected_at is not None:
                    self.reconnects += 1
                if connected_at is not None and time.monotonic() - connected_at >= 5:
                    retry_s = 0.1
                log.warning("reference ws reconnect in %.1fs: %s: %s",
                            retry_s, exc.__class__.__name__, exc)
                await asyncio.sleep(retry_s)
                retry_s = min(2.0, retry_s * 2)
