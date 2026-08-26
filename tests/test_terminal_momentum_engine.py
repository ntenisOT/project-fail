from __future__ import annotations

import pytest

from paper.cohort_engine import CohortEngine
from paper.momentum_engine import TerminalMomentumWindow
from paper.order_book import OrderBook
from paper.pair_types import PairConfig


def _book(bid: float, ask: float, size: float = 50.0) -> OrderBook:
    return OrderBook(
        bids={bid: size}, asks={ask: size}, tick=0.01, min_order_size=5.0,
    )


def _config(**overrides: object) -> PairConfig:
    values: dict[str, object] = {
        "action_latency_s": 0.315,
        "clip_shares": 5.0,
        "max_inventory": 10.0,
        "momentum_threshold": 0.10,
        "momentum_lookback_s": 10.0,
        "momentum_chase_ticks": 1,
        "momentum_cooldown_s": 20.0,
        "momentum_max_entries": 2,
        "new_pair_start_s": 10.0,
    }
    values.update(overrides)
    return PairConfig(
        "terminal10", "terminal_momentum", 0.02, **values,  # type: ignore[arg-type]
    )


def _window(**overrides: object) -> TerminalMomentumWindow:
    return TerminalMomentumWindow(
        _config(**overrides), "btc", "btc-updown-5m-1000", 1000,
        "UP", "DOWN", 1000,
    )


def test_signal_waits_for_taker_latency_then_holds_to_settlement() -> None:
    window = _window()
    flat = _book(0.49, 0.51)
    window.on_books(1001.0, flat, flat)

    signal_records = window.on_books(1012.0, _book(0.61, 0.63), flat)

    assert signal_records == []
    assert window.terminal_signals == 1
    assert window.terminal_entries == 0
    assert window.inventory[True] == 0
    assert window.on_books(1012.300, _book(0.62, 0.64), flat) == []

    fills = window.on_books(1012.315, _book(0.62, 0.64), flat)

    assert window.terminal_entries == 1
    assert window.inventory[True] == 5
    assert window.inventory[False] == 0
    assert window.sells == 0
    assert window.taker_fees > 0
    assert [record["action"] for record in fills] == ["terminal_momentum_buy"]
    assert float(fills[0]["price"]) == 0.64
    CohortEngine._fill_record(
        1012.315, "terminal10", "btc", window.slug, fills[0],
    )

    window.on_books(1290.0, _book(0.90, 0.92), flat)
    assert window.sells == 0, "winner-like inventory must remain open to settlement"
    settlement, metrics = window.settle(1300.0, 1)
    assert settlement["resid_shares"] == 5
    assert settlement["pnl"] > 0
    assert metrics["terminal_momentum_entries"] == 1
    assert metrics["terminal_momentum_blocked"] == 0


def test_one_tick_cap_blocks_a_repriced_ask_without_retrying() -> None:
    window = _window()
    flat = _book(0.49, 0.51)
    window.on_books(1001.0, flat, flat)
    window.on_books(1012.0, _book(0.61, 0.63), flat)  # cap is 0.64

    fills = window.on_books(1012.315, _book(0.64, 0.65), flat)

    assert fills == []
    assert window.terminal_entries == 0
    assert window.terminal_blocked == 1
    assert window.inventory[True] == 0
    assert window.on_books(1013.0, _book(0.64, 0.65), flat) == []
    assert window.terminal_signals == 1, "cooldown must suppress immediate resignal"


def test_capped_sweep_can_use_multiple_levels_but_never_cross_the_cap() -> None:
    window = _window()
    flat = _book(0.49, 0.51)
    window.on_books(1001.0, flat, flat)
    window.on_books(1012.0, _book(0.61, 0.63), flat)  # cap is 0.64
    delayed = OrderBook(
        bids={0.62: 50}, asks={0.63: 3, 0.64: 4, 0.65: 50},
        tick=0.01, min_order_size=5.0,
    )

    fills = window.on_books(1012.315, delayed, flat)

    assert sum(float(record["size"]) for record in fills) == pytest.approx(5)
    assert max(float(record["price"]) for record in fills) == 0.64
    assert all(float(record["price"]) <= 0.64 for record in fills)


def test_candidate_never_posts_maker_quotes() -> None:
    window = _window()
    flat = _book(0.49, 0.51)
    for second in range(1001, 1012):
        window.on_books(float(second), flat, flat)
    assert window.orders == {}
    assert window.quote_posts == 0
    assert window.maker_rebates == 0
