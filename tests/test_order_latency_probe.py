from paper.market_metadata import ActiveMarket
from live.order_probe import choose_probe_order


def test_probe_is_distant_five_share_buy_under_hard_cap() -> None:
    market = ActiveMarket(
        "btc", "btc-updown-5m-0", 0, "0x" + "1" * 64, "up", "down", 5,
    )
    books = {
        "up": {"bids": [{"price": "0.58"}], "tick_size": "0.01", "min_order_size": "5"},
        "down": {"bids": [{"price": "0.41"}], "tick_size": "0.01", "min_order_size": "5"},
    }

    plan = choose_probe_order(market, books, 5.0)

    assert (plan.outcome, plan.price, plan.size) == ("Up", 0.38, 5.0)
    assert 1 <= plan.notional <= 5
