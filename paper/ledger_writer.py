"""Single-owner background writer for the paper SQLite ledger."""

from __future__ import annotations

import queue
import threading
from typing import Any

from paper.ledger import Ledger

Message = tuple[str, tuple[object, ...]]


class LedgerWriter:
    def __init__(self, path: str = "paper/paper.db", capacity: int = 4096) -> None:
        self._messages: queue.Queue[Message | None] = queue.Queue(maxsize=capacity)
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, args=(path,), name="paper-ledger", daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5) or self._error is not None:
            raise RuntimeError("paper ledger writer failed to start") from self._error

    def _run(self, path: str) -> None:
        ledger: Ledger | None = None
        try:
            ledger = Ledger(path)
            self._ready.set()
            while True:
                message = self._messages.get()
                if message is None:
                    break
                method, args = message
                getattr(ledger, method)(*args)
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            if ledger is not None:
                ledger.close()

    def _put(self, method: str, *args: object) -> None:
        if self._closed or self._error is not None:
            raise RuntimeError("paper ledger writer is unavailable") from self._error
        try:
            self._messages.put_nowait((method, args))
        except queue.Full as exc:
            raise RuntimeError("paper ledger queue is full") from exc

    def record_fill(self, ts: float, strategy: str, asset: str, slug: str,
                    record: dict[str, Any]) -> None:
        self._put("record_fill", ts, strategy, asset, slug, record)

    def record_reference(self, asset: str, observed_at: float, received_at: float,
                         value_e18: str, window_s: int) -> None:
        self._put(
            "record_reference", asset, observed_at, received_at, value_e18, window_s,
        )

    def record_settlement(self, ts: float, strategy: str, asset: str, slug: str,
                          settlement: dict[str, Any]) -> None:
        self._put("record_settlement", ts, strategy, asset, slug, settlement)

    def record_resolved_window(self, ts: float, asset: str, slug: str,
                               outcome_up: int) -> None:
        self._put("record_resolved_window", ts, asset, slug, outcome_up)

    def record_metrics(self, ts: float, strategy: str, asset: str, slug: str,
                       metrics: dict[str, object]) -> None:
        self._put("record_metrics", ts, strategy, asset, slug, metrics)

    def record_invalid_window(self, ts: float, strategy: str, asset: str, slug: str,
                              invalid: dict[str, Any]) -> None:
        self._put("record_invalid_window", ts, strategy, asset, slug, invalid)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._error is None:
            self._messages.put(None, timeout=5)
        self._thread.join(timeout=10)
        if self._thread.is_alive() or self._error is not None:
            raise RuntimeError("paper ledger writer failed") from self._error
