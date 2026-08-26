"""A causal, bounded view of the official Chainlink TWAP for trading decisions.

Until now the reference feed only wrote to the ledger - the engine never saw
it, and the report says so explicitly ("causal and not used for orders").
tools/twap_observability.py measured why that is worth changing: the sign of
the partial signal already agrees with the settled outcome 93.1% of the time
with 60 seconds left, because settlement is a 60-second AVERAGE and half of it
has already happened.

The one thing this class must never do is let a strategy see a value before it
was observed. Every read is bounded by an explicit `now`, and samples at or
after `now` are invisible. A window that could peek even one second ahead
would manufacture the entire result.
"""
from __future__ import annotations

import bisect

RETAIN_S = 900.0


class ReferenceView:
    """Latest and historical TWAP values per asset, readable only causally."""

    def __init__(self, retain_s: float = RETAIN_S) -> None:
        if retain_s <= 0:
            raise ValueError("retain_s must be positive")
        self.retain_s = retain_s
        self._times: dict[str, list[float]] = {}
        self._values: dict[str, list[float]] = {}

    def update(self, asset: str, observed_at: float, value: float) -> None:
        if value <= 0:
            return
        times = self._times.setdefault(asset, [])
        values = self._values.setdefault(asset, [])
        # feeds can repeat or reorder; keep the series sorted and deduplicated
        position = bisect.bisect_left(times, observed_at)
        if position < len(times) and times[position] == observed_at:
            values[position] = value
            return
        times.insert(position, observed_at)
        values.insert(position, value)
        cutoff = observed_at - self.retain_s
        drop = bisect.bisect_left(times, cutoff)
        if drop:
            del times[:drop]
            del values[:drop]

    def at(self, asset: str, when: float, *, now: float,
           tolerance: float = 4.0) -> float | None:
        """The value observed nearest `when`, never later than `now`.

        `now` is mandatory: a strategy asking for the opening value must not be
        handed a sample the feed had not yet published.
        """
        if when > now:
            # Not a future leak - tolerance would only return an older sample -
            # but handing back a stale value for a moment that has not happened
            # hides the caller's mistake. Refuse instead.
            return None
        times = self._times.get(asset)
        if not times:
            return None
        values = self._values[asset]
        limit = bisect.bisect_right(times, now)
        if limit == 0:
            return None
        best_index: int | None = None
        best_gap = tolerance
        position = bisect.bisect_left(times, when, 0, limit)
        for index in (position - 1, position):
            if 0 <= index < limit:
                gap = abs(times[index] - when)
                if gap <= best_gap:
                    best_gap, best_index = gap, index
        return None if best_index is None else values[best_index]

    def latest(self, asset: str, *, now: float,
               max_age_s: float = 10.0) -> tuple[float, float] | None:
        """(observed_at, value) of the freshest sample at or before `now`."""
        times = self._times.get(asset)
        if not times:
            return None
        limit = bisect.bisect_right(times, now)
        if limit == 0:
            return None
        observed_at = times[limit - 1]
        if now - observed_at > max_age_s:
            return None
        return observed_at, self._values[asset][limit - 1]

    def signal_bps(self, asset: str, opening_at: float, *, now: float,
                   max_age_s: float = 10.0) -> float | None:
        """Basis points of the current TWAP against the window's opening TWAP.

        This is the same quantity paper/reference_report.py audits at T+30,
        computed here for a live decision instead of an after-the-fact report.
        """
        opening = self.at(asset, opening_at, now=now)
        if opening is None or opening <= 0:
            return None
        current = self.latest(asset, now=now, max_age_s=max_age_s)
        if current is None:
            return None
        return (current[1] / opening - 1.0) * 10_000.0
