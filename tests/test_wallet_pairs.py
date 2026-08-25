from tools.wallet_pairs import BuyFill, summarize_pairs


def test_fifo_pairing_reports_cost_coverage_delay_and_residual() -> None:
    wallet = "0x" + "1" * 40
    fills = [
        BuyFill(wallet, "btc-1", 1, (1, 1), 10, 10, 0.40, True),
        BuyFill(wallet, "btc-1", 0, (1, 2), 12, 5, 0.50, True),
        BuyFill(wallet, "btc-1", 0, (1, 3), 20, 10, 0.61, False, 0.10),
    ]

    row = summarize_pairs(fills, wallet)

    assert row.paired_shares == 10
    assert row.completion_pct == 80
    assert abs((row.average_sum or 0) - 0.96) < 1e-9
    assert row.under_98_shares == 5
    assert row.both_maker_shares == 5
    assert row.residual_shares == 5
    assert row.median_delay_s == 2
    assert row.p90_delay_s == 10
