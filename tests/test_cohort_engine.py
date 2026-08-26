from __future__ import annotations

from collections.abc import Sequence

from paper.cohort_engine import (
    CohortEngine,
    CohortRecord,
    FillRecord,
    InvalidWindowRecord,
)
from paper.market_metadata import ActiveMarket
from paper.pair_types import PairConfig


def _market(start: int = 0) -> ActiveMarket:
    return ActiveMarket(
        "btc", f"btc-updown-5m-{start}", start, "condition",
        f"up-{start}", f"down-{start}", 5,
    )


def _config(name: str, latency: float = 0) -> PairConfig:
    return PairConfig(
        name, "accumulate", 0.01, action_latency_s=latency,
        buy_sum_ceiling=0.99, max_inventory=5,
    )


def _book(token: str, timestamp: float) -> dict[str, object]:
    bid, ask = ((0.48, 0.52) if token.startswith("up") else (0.49, 0.51))
    return {
        "event_type": "book", "asset_id": token, "timestamp": timestamp,
        "bids": [{"price": bid, "size": 1}],
        "asks": [{"price": ask, "size": 5}],
    }


def _trade(token: str, timestamp: float) -> dict[str, object]:
    price = 0.48 if token.startswith("up") else 0.49
    return {
        "event_type": "last_trade_price", "asset_id": token,
        "timestamp": timestamp, "side": "SELL", "price": price, "size": 6,
    }


def _fresh_books(engine: CohortEngine, timestamp: float = 1) -> None:
    engine.on_event(_book("up-0", timestamp), timestamp)
    engine.on_event(_book("down-0", timestamp), timestamp)


def test_rejected_delta_breaks_chain_until_authoritative_snapshot() -> None:
    engine = CohortEngine((_config("pair"),))
    engine.open_market(_market(), 0)
    engine.on_event(_book("up-0", 1.2), 1.2)
    engine.on_event(_book("down-0", 1.2), 1.2)
    stale_down = {
        "event_type": "price_change", "timestamp": 1,
        "price_changes": [
            {"asset_id": "down-0", "side": "BUY", "price": 0.20, "size": 1},
            {"asset_id": "down-0", "side": "SELL", "price": 0.21, "size": 5},
        ],
    }
    engine.on_event(stale_down, 1.5)
    assert engine.tick(1.5) == ()
    down = engine.books.get("down-0")
    assert down is not None
    assert not down.bootstrapped
    assert down.best_ask is None

    fresh_down = {
        "event_type": "price_change", "timestamp": 1.51,
        "price_changes": [
            {"asset_id": "down-0", "side": "BUY", "price": 0.49, "size": 1},
        ],
    }
    engine.on_event(fresh_down, 1.51)
    assert engine.runtime_snapshot()["stale_assets"] == ["btc"]
    engine.on_event(_book("down-0", 1.52), 1.52)
    engine.tick(1.52)

    fills = engine.on_event(_trade("up-0", 1.53), 1.53)
    assert [(row.strategy, row.size) for row in fills] == [("pair", 5)]


def test_deltas_cannot_bootstrap_a_book_after_reset() -> None:
    engine = CohortEngine((_config("pair"),))
    engine.open_market(_market(), 0)
    delta = {
        "event_type": "price_change", "timestamp": 1,
        "price_changes": [
            {"asset_id": "up-0", "side": "BUY", "price": 0.48, "size": 1},
            {"asset_id": "down-0", "side": "BUY", "price": 0.49, "size": 1},
        ],
    }
    engine.on_event(delta, 1)
    assert engine.tick(1) == ()
    assert engine.runtime_snapshot()["stale_assets"] == ["btc"]

    _fresh_books(engine, 1.1)
    engine.tick(1.1)
    assert engine.runtime_snapshot()["stale_assets"] == []


def test_unattributable_malformed_delta_invalidates_every_active_book() -> None:
    engine = CohortEngine((_config("pair"),))
    engine.open_market(_market(), 0)
    _fresh_books(engine, 1)
    engine.on_event({
        "event_type": "price_change", "timestamp": 1.1,
        "price_changes": [
            {"asset_id": "up-0", "side": "BUY", "price": 0.48, "size": 0},
            {"side": "SELL", "price": 0.52, "size": 0},
        ],
    }, 1.1)
    assert engine.runtime_snapshot()["stale_assets"] == ["btc"]
    assert not engine.books.get("up-0").bootstrapped  # type: ignore[union-attr]
    assert not engine.books.get("down-0").bootstrapped  # type: ignore[union-attr]
    engine.on_event(_book("up-0", 1.2), 1.2)
    assert engine.runtime_snapshot()["stale_assets"] == ["btc"]


def test_book_freshness_expires_during_feed_silence() -> None:
    engine = CohortEngine((_config("pair"),), max_event_lag_s=0.4)
    engine.open_market(_market(), 0)
    engine.on_event(_book("up-0", 1), 1.39)
    engine.on_event(_book("down-0", 1), 1.39)
    engine.on_event({
        "event_type": "tick_size_change", "asset_id": "up-0",
        "new_tick_size": "0.001",
    }, 1.4)
    engine.tick(1.39)
    assert engine.runtime_snapshot()["stale_assets"] == []

    engine.tick(1.401)
    assert engine.runtime_snapshot()["stale_assets"] == ["btc"]


def test_trade_causality_respects_pre_and_post_activation_times() -> None:
    engine = CohortEngine((_config("latency", 0.065),))
    engine.open_market(_market(), 0)
    _fresh_books(engine)
    engine.tick(1)

    assert engine.on_event(_trade("up-0", 1.03), 1.03) == ()
    assert engine.tick(1.064) == ()
    engine.tick(1.065)

    fills = engine.on_event(_trade("up-0", 1.066), 1.1)
    assert len(fills) == 1
    assert fills[0].ts == 1.066
    assert fills[0].strategy == "latency"


def test_future_dated_feed_invalidates_instead_of_creating_fills() -> None:
    engine = CohortEngine((_config("clock"),))
    engine.open_market(_market(), 0)
    engine.on_event(_book("up-0", 1.2), 1.0)
    engine.on_event(_book("down-0", 1.2), 1.0)
    engine.tick(1.0)
    assert engine.on_event(_trade("up-0", 1.3), 1.0) == ()

    engine.finish_window("btc", 300)
    records = engine.settle("btc", 1, 315)
    assert len(records) == 1
    assert isinstance(records[0], InvalidWindowRecord)
    assert records[0].reason == "future_market_timestamp"


def test_disconnect_invalidates_then_finishes_and_settles_once() -> None:
    engine = CohortEngine((_config("pair"),))
    engine.open_market(_market(), 0)
    _fresh_books(engine)
    engine.tick(1)
    assert len(engine.on_event(_trade("up-0", 1.1), 1.1)) == 1

    engine.disconnect(1.2)
    assert engine.on_event(_trade("down-0", 1.3), 1.3) == ()
    engine.finish_window("btc", 300)
    records = engine.settle("btc", 1, 315)

    assert len(records) == 1
    assert isinstance(records[0], InvalidWindowRecord)
    assert records[0].reason == "ws_reconnect"
    assert records[0].n_fills == 1


def _run(configs: Sequence[PairConfig]) -> tuple[CohortRecord, ...]:
    engine = CohortEngine(configs)
    engine.open_market(_market(), 0)
    _fresh_books(engine)
    engine.tick(1)
    records: list[CohortRecord] = []
    records.extend(engine.on_event(_trade("up-0", 1.1), 1.1))
    records.extend(engine.on_event(_trade("down-0", 1.2), 1.2))
    engine.finish_window("btc", 300)
    records.extend(engine.settle("btc", 1, 315))
    return tuple(records)


def test_cohort_isolation_and_repeated_replay_are_deterministic() -> None:
    alpha, beta = _config("alpha"), _config("beta")
    together = _run((alpha, beta))

    assert together == _run((alpha, beta))
    assert tuple(row for row in together if row.strategy == "alpha") == _run((alpha,))
    assert tuple(row for row in together if row.strategy == "beta") == _run((beta,))
    assert sum(isinstance(row, FillRecord) for row in together) == 4
