from paper.order_book import OrderBook
from paper.pair_engine import PairWindow
from paper.pair_types import PairConfig


def book(bid: float, ask: float) -> OrderBook:
    return OrderBook({bid: 0}, {ask: 20}, 1)


def test_fee_aware_taker_completion_uses_only_earned_basket_surplus() -> None:
    config = PairConfig(
        "taker", "accumulate", 0.01, 0, clip_shares=10, max_inventory=40,
        buy_sum_ceiling=0.99, basket_average_cap=True, buy_taker_after_s=5,
    )
    up = book(0.30, 0.31)
    affordable = book(0.60, 0.65)
    window = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
    window.on_books(1, up, affordable)
    assert window.on_trade(2, True, 0.30, 10, "SELL") is not None

    fills = window.on_books(5, up, affordable)

    assert [fill["action"] for fill in fills] == ["taker_buy"]
    assert window.inventory == {True: 10, False: 10}
    _, metrics = window.settle(300, 1)
    assert float(metrics["taker_fees"]) > 0
    assert float(metrics["buy_pair_cost"]) / float(metrics["buy_pair_shares"]) <= 0.99

    expensive = book(0.60, 0.75)
    refused = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
    refused.on_books(1, up, expensive)
    assert refused.on_trade(2, True, 0.30, 10, "SELL") is not None
    assert not refused.on_books(5, up, expensive)
    assert refused.inventory[False] == 0
