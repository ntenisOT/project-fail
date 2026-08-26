from __future__ import annotations

import pytest

from paper.cohort_engine import CohortEngine
from paper.market_metadata import ActiveMarket
from tools.pair_completion_counterfactual import completion_counterfactual_board
from tools.pair_completion_opportunity_census import OpportunityCensus, _state, _window


def _setup() -> tuple[CohortEngine, OpportunityCensus]:
    baseline, completion = completion_counterfactual_board(0.065)
    engine = CohortEngine((baseline,))
    engine.open_market(
        ActiveMarket("btc", "btc-updown-5m-0", 0, "condition", "up", "down", 5),
        0,
    )
    for token, bid, ask in (("up", 0.47, 0.50), ("down", 0.47, 0.49)):
        engine.on_event({
            "event_type": "book", "asset_id": token, "timestamp": 1,
            "bids": [{"price": bid, "size": 1}],
            "asks": [{"price": ask, "size": 5}],
        }, 1)
    engine.tick(1)
    engine.tick(30)
    engine.tick(30.066)
    return engine, OpportunityCensus(completion)


def _first_leg(engine: CohortEngine, census: OpportunityCensus) -> None:
    window = _window(engine, "btc")
    before = _state(window)
    fills = engine.on_event({
        "event_type": "last_trade_price", "asset_id": "up",
        "timestamp": 30.067, "side": "SELL", "price": 0.48, "size": 5,
    }, 30.067)
    assert len(fills) == 1
    census.after_fill(window, before, _state(window), fills[0], 30.067)


def test_census_records_first_latency_valid_fee_and_spread_opportunity() -> None:
    engine, census = _setup()
    _first_leg(engine, census)
    window = _window(engine, "btc")
    cohort = engine._active["btc"]
    up = engine.books.get(cohort.market.up_token)
    down = engine.books.get(cohort.market.down_token)
    assert up is not None and down is not None

    assert not census.observe_tick(window, up, down, 30.131)
    assert census.observe_tick(window, up, down, 30.133)
    row = census.rows[0]
    opportunity = row["opportunity"]

    assert isinstance(opportunity, dict)
    assert row["endpoint"] == "taker_completion_opportunity"
    assert opportunity["cumulative_pair_average_after"] == pytest.approx(0.987494)
    assert opportunity["taker_fee_usd"] == pytest.approx(0.08747)
    assert opportunity["spread_cross_cost_usd"] == pytest.approx(0.05)
    assert opportunity["fee_plus_spread_insurance_cost_usd"] == pytest.approx(0.13747)
    assert opportunity["reentry_state_enabled"] is True


def test_natural_maker_completion_wins_before_taker_eligibility() -> None:
    engine, census = _setup()
    _first_leg(engine, census)
    window = _window(engine, "btc")
    before = _state(window)
    fills = engine.on_event({
        "event_type": "last_trade_price", "asset_id": "down",
        "timestamp": 30.10, "side": "SELL", "price": 0.48, "size": 5,
    }, 30.10)
    assert len(fills) == 1
    census.after_fill(window, before, _state(window), fills[0], 30.10)

    assert census.rows[0]["endpoint"] == "natural_maker_completion"
    assert census.rows[0]["known_seconds_to_endpoint"] == pytest.approx(0.033)
    assert census.rows[0]["opportunity"] is None
