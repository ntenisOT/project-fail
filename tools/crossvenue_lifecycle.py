"""Connection-attempt accounting for cross-venue capture provenance."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class ConnectionLifecycle:
    attempts: int = 0
    connections: int = 0
    reconnects: int = 0
    disconnects: int = 0
    preconnect_failures: int = 0
    current_connected: bool = False

    def begin_attempt(self) -> int:
        self.attempts += 1
        self.current_connected = False
        return self.attempts

    def mark_connected(self) -> None:
        if self.current_connected:
            raise RuntimeError("connection attempt was marked connected twice")
        self.current_connected = True
        self.connections += 1

    def mark_failure(self) -> str:
        self.reconnects += 1
        if self.current_connected:
            self.disconnects += 1
            return "source_closed"
        self.preconnect_failures += 1
        return "source_connection_failure"

    def snapshot(self) -> dict[str, int]:
        return {
            "attempts": self.attempts,
            "connections": self.connections,
            "reconnects": self.reconnects,
            "disconnects": self.disconnects,
            "preconnect_failures": self.preconnect_failures,
        }
