import math

from paper.pair_lots import PairLots


def test_fifo_pair_lots_preserve_cost_and_share_weighted_completion_delay() -> None:
    pairs = PairLots()
    pairs.add(True, 3, 0.40, 10)
    pairs.add(True, 2, 0.45, 20)
    pairs.add(False, 4, 0.55, 30)
    pairs.add(False, 1, 0.50, 50)

    assert pairs.paired_shares == 5
    assert math.isclose(pairs.paired_value, 4.8)
    assert pairs.open_side is None
    assert pairs.delay_quantile(0.5) == 20
    assert pairs.delay_quantile(0.9) == 30
