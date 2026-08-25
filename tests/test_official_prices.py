from __future__ import annotations

import dataclasses
import json

from tools.market_windows import ResolvedWindow
from tools.official_prices import OfficialPrice, load_price_cache, official_prices, parse_gamma_price


def _window(winner_up: int = 1) -> ResolvedWindow:
    return ResolvedWindow(
        "btc-updown-5m-1787691000", "btc", 1787691000,
        "0x" + "1" * 64, "11", "22", winner_up,
    )


def test_parse_gamma_price_requires_exact_twap_config_and_outcome() -> None:
    window = _window()
    row = parse_gamma_price(window, [{
        "eventMetadata": {"priceToBeat": "78209.30424402266",
                          "finalPrice": "78283.19677935798"},
        "markets": [{
            "slug": window.slug,
            "cryptoMarketConfigId": "btc-5m-twap-60",
            "cryptoMarketConfig": {
                "id": "btc-5m-twap-60", "twapEnabled": True,
                "twapLookbackSeconds": 60,
            },
        }],
    }])
    assert row.price_to_beat == "78209.30424402266"
    assert row.final_price == "78283.19677935798"
    assert row.winner_up == 1
    payload = [{
        "eventMetadata": {"priceToBeat": "100", "finalPrice": "101"},
        "markets": [{
            "slug": window.slug, "cryptoMarketConfigId": "btc-5m-twap-30",
            "cryptoMarketConfig": {"twapEnabled": True, "twapLookbackSeconds": 30},
        }],
    }]
    assert parse_gamma_price(window, payload).lookback_s == 30


def test_parse_gamma_price_rejects_outcome_contradiction() -> None:
    window = _window(winner_up=0)
    payload = [{
        "eventMetadata": {"priceToBeat": "100", "finalPrice": "101"},
        "markets": [{
            "slug": window.slug,
            "cryptoMarketConfigId": "btc-5m-twap-60",
            "cryptoMarketConfig": {
                "twapEnabled": True, "twapLookbackSeconds": 60,
            },
        }],
    }]
    try:
        parse_gamma_price(window, payload)
    except ValueError as exc:
        assert "contradict" in str(exc)
    else:
        raise AssertionError("outcome contradiction was accepted")


def test_official_prices_checkpoints_successes_and_reports_missing(
    tmp_path, monkeypatch,
) -> None:
    good, bad = _window(), _window(winner_up=0)
    bad = ResolvedWindow(
        bad.slug.replace("1787691000", "1787691300"), bad.asset, 1787691300,
        bad.condition_id, "33", "44", bad.winner_up,
    )

    def fake_fetch(window: ResolvedWindow) -> OfficialPrice:
        if window == bad:
            raise RuntimeError("metadata absent")
        return OfficialPrice(window.slug, "100", "101", "btc-5m-twap-60", 60)

    monkeypatch.setattr("tools.official_prices.fetch_gamma_price", fake_fetch)
    cache = tmp_path / "prices.jsonl"
    rows = official_prices(
        [good, bad], cache, workers=1, allow_missing=True,
        min_request_interval_s=0, checkpoint_every=1,
    )

    assert list(rows) == [good.slug]
    assert list(load_price_cache(cache)) == [good.slug]
    errors = cache.with_suffix(cache.suffix + ".errors.jsonl").read_text()
    assert bad.slug in errors and "metadata absent" in errors


def test_official_prices_clears_stale_errors_when_cache_is_complete(
    tmp_path,
) -> None:
    window = _window()
    cache = tmp_path / "prices.jsonl"
    errors = cache.with_suffix(cache.suffix + ".errors.jsonl")
    row = OfficialPrice(window.slug, "100", "101", "btc-5m-twap-60", 60)
    cache.write_text(json.dumps(dataclasses.asdict(row)) + "\n", encoding="utf-8")
    errors.write_text('{"slug":"stale","error":"old"}\n', encoding="utf-8")

    result = official_prices([window], cache, fetch_missing=True)

    assert result == {window.slug: row}
    assert errors.read_text(encoding="utf-8") == ""
