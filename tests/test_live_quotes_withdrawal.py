from __future__ import annotations

import unittest

from paper.cohort_engine import CohortEngine
from paper.market_metadata import ActiveMarket
from paper.pair_types import PairConfig


def _market(start: int = 0) -> ActiveMarket:
    return ActiveMarket(
        "btc", f"btc-updown-5m-{start}", start, "condition",
        f"up-{start}", f"down-{start}", 5,
    )


def _config(name: str = "basket99") -> PairConfig:
    return PairConfig(
        name, "accumulate", 0.01, action_latency_s=0,
        buy_sum_ceiling=0.99, max_inventory=5,
    )


def _book(token: str, timestamp: float) -> dict[str, object]:
    bid, ask = ((0.48, 0.52) if token.startswith("up") else (0.49, 0.51))
    return {
        "event_type": "book", "asset_id": token, "timestamp": timestamp,
        "bids": [{"price": bid, "size": 1}],
        "asks": [{"price": ask, "size": 5}],
    }


class LiveQuoteWithdrawalTests(unittest.TestCase):
    """A quote the paper board drops must be withdrawn on the live bridge.

    live/executor.py keeps its last desired quote per (strategy, token) until
    STALE_INTENT_S = 240s. If live_quotes() simply stops mentioning a token,
    the executor holds that quote on the real book for up to four minutes
    after the paper board withdrew it in 65ms. These tests drive a real engine
    through a real transition rather than asserting on counters.
    """

    def _engine_with_quotes(self) -> CohortEngine:
        engine = CohortEngine((_config(),))
        engine.open_market(_market(), 0)
        engine.on_event(_book("up-0", 1), 1)
        engine.on_event(_book("down-0", 1), 1)
        engine.tick(1)
        return engine

    def test_engine_actually_quotes_first(self) -> None:
        """Guard: if this fails the other tests prove nothing."""
        quotes = self._engine_with_quotes().live_quotes()
        self.assertTrue(quotes, "engine posted no quotes; rest of suite is vacuous")
        self.assertTrue(any(q.get("bid") is not None for q in quotes))

    def test_retired_market_emits_withdrawal_for_every_published_token(self) -> None:
        engine = self._engine_with_quotes()
        published = {(q["strategy"], q["token"]) for q in engine.live_quotes()}
        self.assertTrue(published)

        engine.finish_window("btc", 300)          # market gone
        withdrawals = engine.live_quotes()

        self.assertEqual(
            {(q["strategy"], q["token"]) for q in withdrawals}, published,
            "every previously published token must be explicitly withdrawn")
        for record in withdrawals:
            self.assertTrue(record["withdraw"])
            self.assertIsNone(record["bid"], "withdrawal must cancel the bid")
            self.assertIsNone(record["ask"], "withdrawal must cancel the ask")
            self.assertEqual(record["bid_shares"], 0.0)
            self.assertEqual(record["ask_shares"], 0.0)

    def test_withdrawal_is_emitted_once_not_repeated_forever(self) -> None:
        engine = self._engine_with_quotes()
        engine.live_quotes()
        engine.finish_window("btc", 300)
        self.assertTrue(engine.live_quotes(), "first call must withdraw")
        self.assertEqual(engine.live_quotes(), [],
                         "withdrawal must not repeat every loop")

    def test_withdrawal_keeps_the_executor_contract_keys(self) -> None:
        """The executor reads these keys positionally; a withdrawal is not
        allowed to be a differently-shaped record."""
        engine = self._engine_with_quotes()
        engine.live_quotes()
        engine.finish_window("btc", 300)
        for record in engine.live_quotes():
            for key in ("strategy", "token", "slug",
                        "bid", "bid_shares", "ask", "ask_shares"):
                self.assertIn(key, record)

    def test_live_gate_forwards_withdrawals_for_enabled_strategies(self) -> None:
        """A gate that filtered withdrawals out would reintroduce the bug."""
        import json
        import os
        import tempfile
        from pathlib import Path

        from paper.live_gate import LiveGate

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "live.json"
            config.write_text(json.dumps({"enabled": ["basket99"]}), encoding="utf-8")
            prior = os.environ.get("PAPER_LIVE_INTENTS")
            os.environ["PAPER_LIVE_INTENTS"] = "1"
            try:
                gate = LiveGate(str(base / "intents.jsonl"), str(config),
                                str(base / "KILL"))
                engine = self._engine_with_quotes()
                gate.emit(engine.live_quotes(), 1000.0)
                engine.finish_window("btc", 300)
                written = gate.emit(engine.live_quotes(), 1100.0)
            finally:
                if prior is None:
                    os.environ.pop("PAPER_LIVE_INTENTS", None)
                else:
                    os.environ["PAPER_LIVE_INTENTS"] = prior

        self.assertGreater(written, 0,
                           "the gate dropped the withdrawal; orders would stay live")


if __name__ == "__main__":
    unittest.main()
