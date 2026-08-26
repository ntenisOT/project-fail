from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from paper.live_gate import LiveGate


QUOTE = {
    "strategy": "basket99", "token": "TOK", "slug": "btc-updown-5m-1000",
    "bid": 0.46, "bid_shares": 5.0, "ask": None, "ask_shares": 0.0,
}


class LiveGateTests(unittest.TestCase):
    """This gate is the only thing between paper quotes and real orders."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.intents = base / "intents.jsonl"
        self.config = base / "live.json"
        self.kill = base / "KILL"
        self._prior = os.environ.get("PAPER_LIVE_INTENTS")
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._prior is None:
            os.environ.pop("PAPER_LIVE_INTENTS", None)
        else:
            os.environ["PAPER_LIVE_INTENTS"] = self._prior

    def _gate(self, *, armed: bool, enabled: list[str] | None = None) -> LiveGate:
        if armed:
            os.environ["PAPER_LIVE_INTENTS"] = "1"
        else:
            os.environ.pop("PAPER_LIVE_INTENTS", None)
        if enabled is not None:
            self.config.write_text(json.dumps({"enabled": enabled}), encoding="utf-8")
        return LiveGate(str(self.intents), str(self.config), str(self.kill))

    def _lines(self) -> list[dict]:
        if not self.intents.exists():
            return []
        return [json.loads(line) for line in
                self.intents.read_text(encoding="utf-8").splitlines() if line]

    # -- fail-closed paths ------------------------------------------------
    def test_disarmed_by_default_writes_nothing(self) -> None:
        gate = self._gate(armed=False, enabled=["basket99"])
        self.assertEqual(gate.emit([QUOTE], 1000.0), 0)
        self.assertEqual(self._lines(), [])

    def test_strategy_not_enabled_writes_nothing(self) -> None:
        gate = self._gate(armed=True, enabled=[])
        self.assertEqual(gate.emit([QUOTE], 1000.0), 0)
        self.assertEqual(self._lines(), [])

    def test_other_strategy_enabled_does_not_leak_this_one(self) -> None:
        gate = self._gate(armed=True, enabled=["basket97"])
        self.assertEqual(gate.emit([QUOTE], 1000.0), 0)
        self.assertEqual(self._lines(), [])

    def test_kill_file_suppresses_everything(self) -> None:
        gate = self._gate(armed=True, enabled=["basket99"])
        self.kill.touch()
        self.assertEqual(gate.emit([QUOTE], 1000.0), 0)
        self.assertEqual(self._lines(), [])
        self.assertEqual(gate.snapshot()["suppressed"], 1)

    def test_unreadable_config_enables_nothing(self) -> None:
        """A corrupt config must never widen permissions."""
        gate = self._gate(armed=True)
        self.config.write_text("{not json", encoding="utf-8")
        self.assertEqual(gate.emit([QUOTE], 1000.0), 0)
        self.assertEqual(self._lines(), [])

    def test_missing_config_enables_nothing(self) -> None:
        gate = self._gate(armed=True)          # no config file written at all
        self.assertEqual(gate.emit([QUOTE], 1000.0), 0)
        self.assertEqual(self._lines(), [])

    def test_quote_without_token_is_skipped(self) -> None:
        gate = self._gate(armed=True, enabled=["basket99"])
        broken = dict(QUOTE)
        broken["token"] = ""
        self.assertEqual(gate.emit([broken], 1000.0), 0)

    # -- the armed path ---------------------------------------------------
    def test_armed_and_enabled_emits_the_executor_contract(self) -> None:
        gate = self._gate(armed=True, enabled=["basket99"])
        self.assertEqual(gate.emit([QUOTE], 1234.5), 1)
        records = self._lines()
        self.assertEqual(len(records), 1)
        record = records[0]
        # exactly the keys live/executor.py reads
        for key in ("strategy", "token", "slug", "ts",
                    "bid", "bid_shares", "ask", "ask_shares"):
            self.assertIn(key, record)
        self.assertEqual(record["strategy"], "basket99")
        self.assertEqual(record["ts"], 1234.5)
        self.assertEqual(record["bid"], 0.46)
        self.assertIsNone(record["ask"])

    def test_appends_rather_than_truncating(self) -> None:
        gate = self._gate(armed=True, enabled=["basket99"])
        gate.emit([QUOTE], 1000.0)
        gate.emit([QUOTE], 1001.0)
        self.assertEqual(len(self._lines()), 2)
        self.assertEqual(gate.snapshot()["emitted"], 2)

    def test_enable_list_is_reloaded_at_runtime(self) -> None:
        gate = self._gate(armed=True, enabled=[])
        self.assertEqual(gate.emit([QUOTE], 1000.0), 0)
        self.config.write_text(json.dumps({"enabled": ["basket99"]}), encoding="utf-8")
        # still inside the reload window: must stay closed
        self.assertEqual(gate.emit([QUOTE], 1005.0), 0)
        # past the reload window: the new list applies
        self.assertEqual(gate.emit([QUOTE], 1100.0), 1)

    def test_kill_beats_an_enabled_strategy(self) -> None:
        """Ordering matters: KILL must win over any enablement."""
        gate = self._gate(armed=True, enabled=["basket99"])
        self.assertEqual(gate.emit([QUOTE], 1000.0), 1)
        self.kill.touch()
        self.assertEqual(gate.emit([QUOTE], 1100.0), 0)
        self.assertEqual(len(self._lines()), 1)


if __name__ == "__main__":
    unittest.main()
