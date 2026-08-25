from __future__ import annotations

import json

from tools.crossvenue_capture import SourceStats, arguments
from tools.crossvenue_sources import parse_frame, source_specs


def test_source_specs_use_exact_public_topics_and_websockets() -> None:
    specs = {spec.name: spec for spec in source_specs("btc")}
    assert set(specs) == {
        "polymarket_rtds", "binance_spot", "binance_futures", "deribit",
    }
    assert all(spec.url.startswith("wss://") for spec in specs.values())
    subscription = json.loads(specs["polymarket_rtds"].subscribe or "{}")
    row = subscription["subscriptions"][0]
    assert row["topic"] == "crypto_prices_twap_sixty"
    assert row["filters"] == '{"symbol":"btc/usd"}'
    assert "trades.BTC-PERPETUAL.100ms" in (specs["deribit"].subscribe or "")


def test_parsers_preserve_source_and_publisher_time_units() -> None:
    rtds = parse_frame("polymarket_rtds", json.dumps({
        "topic": "crypto_prices_twap_sixty", "timestamp": 1_787_000_000_123,
        "payload": {"timestamp": 1_787_000_000_000},
    }))
    assert rtds.events == 1
    assert rtds.source_time_ns == 1_787_000_000_000_000_000
    assert rtds.publisher_time_ns == 1_787_000_000_123_000_000

    spot = parse_frame("binance_spot", json.dumps({
        "stream": "btcusdt@aggTrade",
        "data": {"e": "aggTrade", "E": 1_787_000_000_124_000,
                 "T": 1_787_000_000_123_000},
    }))
    assert spot.source_time_ns == 1_787_000_000_123_000_000
    assert spot.publisher_time_ns == 1_787_000_000_124_000_000

    futures = parse_frame("binance_futures", json.dumps({
        "stream": "btcusdt@aggTrade",
        "data": {"E": 1_787_000_000_124, "T": 1_787_000_000_123},
    }))
    assert futures.source_time_ns == 1_787_000_000_123_000_000


def test_deribit_batch_and_source_stats_are_measured_causally() -> None:
    parsed = parse_frame("deribit", json.dumps({
        "method": "subscription",
        "params": {"channel": "trades.BTC-PERPETUAL.100ms", "data": [
            {"timestamp": 1_787_000_000_100},
            {"timestamp": 1_787_000_000_120},
        ]},
    }))
    assert parsed.events == 2
    assert parsed.source_time_ns == 1_787_000_000_120_000_000

    stats = SourceStats()
    stats.observe(parsed, 100, 1_787_000_000_130_000_000, 10.0)
    snapshot = stats.snapshot()
    assert snapshot["source_age_p50_ms"] == 10
    assert snapshot["frames"] == 1
    assert snapshot["events"] == 2

    stats.observe(parsed, 100, 1_787_000_000_110_000_000, 11.0)
    snapshot = stats.snapshot()
    assert snapshot["negative_source_age"] == 1
    assert snapshot["min_source_age_ms"] == -10


def test_non_deribit_asset_default_omits_unsupported_source() -> None:
    args = arguments([
        "--asset", "sol", "--label", "smoke", "--output", "out/smoke.jsonl",
    ])
    assert "deribit" not in args.sources
