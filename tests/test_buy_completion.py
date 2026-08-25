import dataclasses

from paper.order_book import OrderBook
from paper.pair_engine import PairWindow
from paper.pair_types import PairConfig


def book(bid: float, ask: float) -> OrderBook:
    return OrderBook({bid: 0}, {ask: 20}, 1)


def test_five_share_taker_completion_is_fee_and_minimum_aware() -> None:
    config = PairConfig(
        "taker", "accumulate", 0.01, 0, clip_shares=5, max_inventory=20,
        buy_sum_ceiling=0.99, basket_average_cap=True, buy_taker_after_s=5,
    )
    up = book(0.30, 0.31)
    affordable = book(0.60, 0.65)
    window = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
    window.on_books(1, up, affordable)
    assert window.on_trade(2, True, 0.30, 5, "SELL") is not None

    fills = window.on_books(5, up, affordable)

    assert [fill["action"] for fill in fills] == ["taker_buy"]
    assert window.inventory == {True: 5, False: 5}
    _, metrics = window.settle(300, 1)
    assert float(metrics["taker_fees"]) > 0
    assert float(metrics["buy_pair_cost"]) / float(metrics["buy_pair_shares"]) <= 0.99

    expensive = book(0.60, 0.75)
    refused = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
    refused.on_books(1, up, expensive)
    assert refused.on_trade(2, True, 0.30, 5, "SELL") is not None
    assert not refused.on_books(5, up, expensive)
    assert refused.inventory[False] == 0

    high = book(0.70, 0.71)
    cheap = book(0.18, 0.19)
    cheap_fill = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
    cheap_fill.on_books(1, high, cheap)
    assert cheap_fill.on_trade(2, True, 0.70, 5, "SELL") is not None
    assert [fill["action"] for fill in cheap_fill.on_books(5, high, cheap)] == [
        "taker_buy"
    ]
    assert cheap_fill.inventory == {True: 5, False: 5}

    partial = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
    partial.on_books(1, high, cheap)
    assert partial.on_trade(2, True, 0.70, 4, "SELL") is not None
    assert not partial.on_books(5, high, cheap)
    assert partial.inventory == {True: 4, False: 0}


def test_taker_completion_can_round_only_near_minimum_dust() -> None:
    config = PairConfig(
        "dust", "accumulate", 0.01, 0, clip_shares=5, max_inventory=5,
        buy_sum_ceiling=0.99, basket_average_cap=True, buy_taker_after_s=5,
        taker_dust_round_shares=0.1,
    )
    up = book(0.30, 0.31)
    down = book(0.60, 0.65)
    rounded = PairWindow(config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
    rounded.on_books(1, up, down)
    assert rounded.on_trade(2, True, 0.30, 5, "SELL") is not None
    assert rounded.on_trade(2.1, False, 0.60, 0.02, "SELL") is not None

    fills = rounded.on_books(5, up, down)

    assert [(fill["action"], fill["size"]) for fill in fills] == [
        ("taker_buy", 5),
    ]
    assert rounded.inventory == {True: 5, False: 5.02}
    settlement, metrics = rounded.settle(300, 1)
    assert settlement["pnl"] > 0
    assert abs(metrics["unmatched_end"] - 0.02) < 1e-9

    far_config = dataclasses.replace(config, max_inventory=20)
    far = PairWindow(far_config, "btc", "btc-updown-5m-0", 0, "up", "down", 0)
    far.on_books(1, up, down)
    assert far.on_trade(2, True, 0.30, 2.04, "SELL") is not None
    assert not far.on_books(5, up, down)
    assert far.inventory == {True: 2.04, False: 0}
