"""Timing helpers for exact five-minute market rotation."""

WINDOW_SECONDS = 300


def boundary_aligned_delay(now: float, max_delay: float = 0.5) -> float:
    """Poll slowly between windows, then wake within 10 ms of the boundary."""
    next_boundary = (int(now) // WINDOW_SECONDS + 1) * WINDOW_SECONDS
    return max(0.01, min(max_delay, next_boundary - now))
