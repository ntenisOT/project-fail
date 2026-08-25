"""Timestamped exposure intervals for feed-quality attribution."""

from __future__ import annotations


class ExposureTimeline:
    """Track when a strategy had orders, pending actions, or an open pair leg."""

    def __init__(self) -> None:
        self._opened_at: float | None = None
        self._closed: list[tuple[float, float]] = []

    def update(self, now: float, exposed: bool) -> None:
        if exposed and self._opened_at is None:
            self._opened_at = now
        elif not exposed and self._opened_at is not None:
            self._closed.append((self._opened_at, max(self._opened_at, now)))
            self._opened_at = None

    def active_at(self, event_at: float | None) -> bool:
        """Return whether an event occurred during measured exposure.

        Missing timestamps remain conservative because their actual occurrence
        cannot be placed within or outside a prior interval.
        """
        if event_at is None:
            return self._opened_at is not None or bool(self._closed)
        if self._opened_at is not None and event_at + 1e-9 >= self._opened_at:
            return True
        return any(start - 1e-9 <= event_at <= end + 1e-9
                   for start, end in self._closed)
