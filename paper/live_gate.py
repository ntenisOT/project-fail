"""Publish the paper board's live quote intents for live/executor.py.

The Gen73 rewrite removed the old gate, so the focused runner has emitted no
intents since then and live/executor.py has had nothing to consume. This
restores that bridge for the paired-bid arms.

It is deliberately fail-closed. Nothing is written unless ALL of these hold:

  * PAPER_LIVE_INTENTS=1 in the environment (default off)
  * the strategy is listed in paper/live.json "enabled" (default [])
  * paper/KILL is absent

The record shape is exactly what the executor already parses:

    {"strategy","token","slug","ts","bid","bid_shares","ask","ask_shares"}

The executor applies its own caps on top (G5 order cap, G7 exposure, G13
per-window spend), so these are *requests*, never authorisations. A paper arm
appearing here does not mean it may trade - live.json enablement is a separate,
deliberate act, and the executor's own mode gate is another.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable

INTENTS = "paper/intents.jsonl"
CONFIG = "paper/live.json"
KILL = "paper/KILL"
CONFIG_RELOAD_S = 10.0

log = logging.getLogger("paper.live_gate")


class LiveGate:
    """Append quote intents for the executor, or do nothing at all."""

    def __init__(self, path: str = INTENTS, config: str = CONFIG,
                 kill: str = KILL) -> None:
        self.path = path
        self.config = config
        self.kill = kill
        self.active = os.environ.get("PAPER_LIVE_INTENTS") == "1"
        self._enabled: frozenset[str] = frozenset()
        self._config_read_at = -1e18
        self.emitted = 0
        self.suppressed = 0
        if self.active:
            log.warning("live gate ACTIVE: intents will be written for strategies "
                        "enabled in %s", self.config)

    def enabled_strategies(self, now: float) -> frozenset[str]:
        """Reload the enable-list periodically so it can be changed at runtime."""
        if now - self._config_read_at < CONFIG_RELOAD_S:
            return self._enabled
        self._config_read_at = now
        try:
            with open(self.config, encoding="utf-8") as handle:
                payload = json.load(handle)
            names = payload.get("enabled") or []
            self._enabled = frozenset(str(name) for name in names)
        except (OSError, ValueError) as exc:
            # an unreadable config must never widen permissions
            self._enabled = frozenset()
            log.warning("live.json unreadable (%s); no strategy is enabled", exc)
        return self._enabled

    def snapshot(self) -> dict[str, int | bool]:
        return {"active": self.active, "emitted": self.emitted,
                "suppressed": self.suppressed, "enabled": len(self._enabled)}

    def emit(self, quotes: Iterable[dict[str, object]], now: float) -> int:
        """Write intents for enabled strategies. Returns the number written."""
        if not self.active:
            return 0
        if os.path.exists(self.kill):
            self.suppressed += 1
            return 0
        allowed = self.enabled_strategies(now)
        if not allowed:
            return 0
        lines = []
        for quote in quotes:
            if quote.get("strategy") not in allowed or not quote.get("token"):
                continue
            record = dict(quote)
            record["ts"] = round(now, 3)
            lines.append(json.dumps(record, separators=(",", ":")))
        if not lines:
            return 0
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError as exc:
            log.warning("intent write failed: %s", exc)
            return 0
        self.emitted += len(lines)
        return len(lines)


def gate() -> LiveGate:
    return LiveGate()
