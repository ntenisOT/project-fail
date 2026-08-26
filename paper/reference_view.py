"""A causal, bounded view of the official Chainlink TWAP for trading decisions.

Until now the reference feed only wrote to the ledger - the engine never saw
it, and the report says so explicitly ("causal and not used for orders").
tools/twap_observability.py measured why that is worth changing: the sign of
the partial signal already agrees with the settled outcome 93.1% of the time
with 60 seconds left, because settlement is a 60-second AVERAGE and half of it
has already happened.

The one thing this class must never do is let a strategy see a value before
it was observed. Every read is bounded by an explicit `now`; a sample stamped
exactly at `now` IS visible (it has just arrived), anything later is not.

Two caveats the review seats raised and this class does NOT solve:
  * It stores `observed_at`, not `received_at`. On the live feed every sample
    arrives late - median 1.678s, p90 2.153s - so a caller passing
    `now = observed_at` would still be reading ahead of delivery. The caller
    must pass a real wall clock, and the opening/latest tolerances here are
    looser than paper/reference_report.py's exact-T+0 requirement.
  * Nothing imports this yet. It is not a live trading path.
"""
from __future__ import annotations

import bisect
import math

RETAIN_S = 900.0
# A sample may legitimately arrive after a long feed gap, so plausibility is
# a separate, wider bound than retention.
MAX_SKEW_S = 300.0


class ReferenceView:
    """Latest and historical TWAP values per asset, readable only causally."""

    def __init__(self, retain_s: float = RETAIN_S,
                 max_skew_s: float = MAX_SKEW_S) -> None:
        if retain_s <= 0:
            raise ValueError("retain_s must be positive")
        if max_skew_s <= 0:
            raise ValueError("max_skew_s must be positive")
        self.retain_s = retain_s
        self.max_skew_s = max_skew_s
        self._times: dict[str, list[float]] = {}
        self._values: dict[str, list[float]] = {}

    def update(self, asset: str, observed_at: float, value: float) -> None:
        if not (value > 0) or not math.isfinite(observed_at):
            # `not (value > 0)` also rejects NaN, which would poison the
            # sorted order and make later reads silently wrong.
            return
        times = self._times.setdefault(asset, [])
        values = self._values.setdefault(asset, [])
        # A sample stamped far in the future must not evict real history.
        # Anchoring retention to the incoming timestamp means one skewed or
        # malformed frame wipes the whole series and the strategy goes blind
        # until the wall clock catches up - a free kill switch for anyone who
        # can influence the feed. A real gap is fine; a 10,000s jump is not.
        if times and observed_at > times[-1] + self.max_skew_s:
            return
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
