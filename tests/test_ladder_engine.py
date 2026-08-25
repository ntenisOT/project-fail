import math

from paper.ladder_engine import LadderWindow
from paper.order_book import OrderBook
from paper.pair_types import PairConfig


def book(bid: float, ask: float) -> OrderBook:
    return OrderBook({bid: 0}, {ask: 20}, 1)


def test_two_level_ladder_holds_queue_and_pairs_cross_level_fills() -> None:
    config = PairConfig(
        "ladder99", "accumulate", 0.01, action_latency_s=0,
        buy_sum_ceiling=0.99, max_inventory=20,
        require_both_to_start=True, basket_average_cap=True,
        ladder_offsets=(0, -1), quote_hold_s=15,
    )
    window = LadderWindow(
        config, "btc", "btc-updown-5m-0", 0, "up", "down", 0,
    )
    initial = book(0.50, 0.52), book(0.48, 0.50)
    window.on_books(1, *initial)
    initial_prices = {key: order.price for key, order in window.orders.items()}
    assert set(initial_prices.values()) == {0.50, 0.49, 0.48, 0.47}

    moved = book(0.51, 0.53), book(0.47, 0.49)
    window.on_books(2, *moved)
    assert {key: order.price for key, order in window.orders.items()} == initial_prices

    up_fill = window.on_trade(3, True, 0.49, 5, "SELL")
    window.on_books(3.1, *moved)
    down_fill = window.on_trade(4, False, 0.46, 5, "SELL")
    assert (up_fill or {})["size"] == 10
    assert (down_fill or {})["size"] == 10

    settled, metrics = window.settle(300, 1)
    assert metrics["buy_pair_shares"] == 10
    pair_cost = metrics["buy_pair_cost"]
    assert isinstance(pair_cost, (int, float))
    assert math.isclose(pair_cost / 10, 0.96)
    assert metrics["unmatched_end"] == 0
    assert math.isclose(float(settled["pnl"]), 0.4)
