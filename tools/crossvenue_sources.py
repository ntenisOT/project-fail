"""Public cross-venue WebSocket definitions and timestamp normalization."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable


@dataclasses.dataclass(frozen=True)
class SourceSpec:
    name: str
    url: str
    subscribe: str | None = None
    application_ping: str | None = None
    application_ping_s: float | None = None


@dataclasses.dataclass(frozen=True)
class ParsedFrame:
    channel: str | None
    events: int
    source_time_ns: int | None
    publisher_time_ns: int | None
    parse_error: bool = False


def _epoch_ns(value: object) -> int | None:
    try:
        timestamp = int(str(value))
    except (TypeError, ValueError):
        return None
    magnitude = abs(timestamp)
    if magnitude >= 100_000_000_000_000_000:
        return timestamp
    if magnitude >= 100_000_000_000_000:
        return timestamp * 1_000
    if magnitude >= 100_000_000_000:
        return timestamp * 1_000_000
    if magnitude >= 1_000_000_000:
        return timestamp * 1_000_000_000
    return None


def _compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def source_specs(asset: str, names: Iterable[str] | None = None) -> list[SourceSpec]:
    asset = asset.lower()
    if asset not in {"btc", "eth", "sol", "xrp"}:
        raise ValueError(f"unsupported asset: {asset}")
    wanted = set(names or ("polymarket_rtds", "binance_spot", "binance_futures",
                           "deribit"))
    unknown = wanted - {"polymarket_rtds", "binance_spot", "binance_futures",
                        "deribit"}
    if unknown:
        raise ValueError(f"unknown sources: {sorted(unknown)}")

    symbol = f"{asset}usdt"
    specs = {
        "polymarket_rtds": SourceSpec(
            "polymarket_rtds", "wss://ws-live-data.polymarket.com",
            _compact({
                "action": "subscribe",
                "subscriptions": [{
                    "topic": "crypto_prices_twap_sixty",
                    "type": "update",
                    "filters": _compact({"symbol": f"{asset}/usd"}),
                }],
            }),
            application_ping="PING", application_ping_s=5,
        ),
        "binance_spot": SourceSpec(
            "binance_spot",
            "wss://data-stream.binance.vision/stream?streams="
            f"{symbol}@aggTrade/{symbol}@bookTicker/{symbol}@kline_1s"
            "&timeUnit=MICROSECOND",
        ),
        "binance_futures": SourceSpec(
            "binance_futures",
            "wss://fstream.binance.com/stream?streams="
            f"{symbol}@aggTrade/{symbol}@bookTicker/{symbol}@markPrice@1s/"
            f"{symbol}@forceOrder",
        ),
    }
    if asset in {"btc", "eth"}:
        upper = asset.upper()
        specs["deribit"] = SourceSpec(
            "deribit", "wss://www.deribit.com/ws/api/v2",
            _compact({
                "jsonrpc": "2.0", "method": "public/subscribe", "id": 1,
                "params": {"channels": [
                    f"trades.{upper}-PERPETUAL.100ms",
                    f"ticker.{upper}-PERPETUAL.100ms",
                    f"deribit_price_index.{asset}_usd",
                    f"deribit_volatility_index.{asset}_usd",
                ]},
            }),
        )
    elif "deribit" in wanted:
        raise ValueError("Deribit capture is currently defined only for BTC and ETH")
    return [specs[name] for name in sorted(wanted)]


def parse_frame(source: str, raw: str | bytes) -> ParsedFrame:
    if raw in ("", b"", "PONG", b"PONG"):
        return ParsedFrame(None, 0, None, None)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return ParsedFrame(None, 0, None, None, parse_error=True)
    if not isinstance(payload, dict):
        return ParsedFrame(None, 0, None, None, parse_error=True)

    if source == "polymarket_rtds":
        body = payload.get("payload")
        if not isinstance(body, dict):
            return ParsedFrame(str(payload.get("topic") or "") or None, 0, None, None)
        return ParsedFrame(
            str(payload.get("topic") or "") or None, 1,
            _epoch_ns(body.get("timestamp")), _epoch_ns(payload.get("timestamp")),
        )

    if source.startswith("binance_"):
        body = payload.get("data", payload)
        if not isinstance(body, dict):
            return ParsedFrame(None, 0, None, None, parse_error=True)
        channel = str(payload.get("stream") or body.get("e") or "") or None
        trade_time = body.get("T")
        if trade_time is None and isinstance(body.get("o"), dict):
            trade_time = body["o"].get("T")
        source_time = _epoch_ns(trade_time if trade_time is not None else body.get("E"))
        return ParsedFrame(channel, 1, source_time, _epoch_ns(body.get("E")))

    if source == "deribit":
        params = payload.get("params")
        if not isinstance(params, dict):
            return ParsedFrame(None, 0, None, None)
        data = params.get("data")
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        timestamps = [_epoch_ns(row.get("timestamp")) for row in rows]
        observed = [timestamp for timestamp in timestamps if timestamp is not None]
        source_time = max(observed) if observed else None
        return ParsedFrame(
            str(params.get("channel") or "") or None, len(rows), source_time, source_time,
        )

    raise ValueError(f"unknown source parser: {source}")
