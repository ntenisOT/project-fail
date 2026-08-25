"""Pure quote planning for paired mint inventory."""

from __future__ import annotations

import dataclasses
import math


@dataclasses.dataclass(frozen=True)
class Quote:
    side_up: bool
    price: float
    size: float


def target_pair_prices(
    ask_up: float,
    ask_down: float,
    *,
    spread: float,
    sum_floor: float,
) -> tuple[float, float]:
    """Return the guarded opposite-ask anchor shared by paper and mintbot."""
    if not (0 < ask_up < 1 and 0 < ask_down < 1):
        raise ValueError("opposite asks must be inside (0, 1)")
    price_up = max(0.05, min(0.95, round(1 - ask_down + spread, 2)))
    price_down = max(0.05, min(0.95, round(1 - ask_up + spread, 2)))
    if price_up + price_down < sum_floor:
        bump = (sum_floor - price_up - price_down) / 2
        price_up = min(0.95, round(price_up + bump + 0.005, 2))
        price_down = min(0.95, round(price_down + bump + 0.005, 2))
    if price_up + price_down + 1e-9 < sum_floor:
        raise ValueError("guarded prices cannot satisfy the pair floor")
    return price_up, price_down


def guarded_pair_prices(
    ask_up: float,
    ask_down: float,
    *,
    spread: float,
    sum_floor: float,
) -> tuple[float, float] | None:
    """Return no quote for endpoint or malformed public-book prices."""
    try:
        return target_pair_prices(
            ask_up, ask_down, spread=spread, sum_floor=sum_floor,
        )
    except ValueError:
        return None


def should_reprice(
    current: tuple[float, float],
    target: tuple[float, float],
    age_seconds: float,
    *,
    tick: float = 0.01,
    hysteresis_ticks: int = 5,
    min_rest_seconds: float = 15.0,
    urgent_ticks: int = 10,
) -> bool:
    """Target durable queue residence, escaping only a severe adverse move."""
    moves = [new - old for old, new in zip(current, target)]
    if max(abs(move) for move in moves) + 1e-9 < hysteresis_ticks * tick:
        return False
    urgent_underpricing = max(moves) + 1e-9 >= urgent_ticks * tick
    return age_seconds >= min_rest_seconds or urgent_underpricing


def plan_pair_quotes(
    *,
    minted: float,
    sold_up: float,
    sold_down: float,
    price_up: float,
    price_down: float,
    sum_floor: float,
    clip_shares: float = 5.0,
    imbalance_tolerance: float = 0.1,
) -> tuple[Quote, ...]:
    """Return a balanced two-order clip, or no orders after asymmetric fills."""
    if not (0.05 <= price_up <= 0.95 and 0.05 <= price_down <= 0.95):
        raise ValueError("quote outside the guarded price range")
    if price_up + price_down + 1e-9 < sum_floor:
        raise ValueError("quote pair is below the set-value floor")
    if abs(sold_up - sold_down) > imbalance_tolerance:
        return ()
    remaining = min(minted - sold_up, minted - sold_down, clip_shares)
    size = math.floor(max(0.0, remaining) * 10) / 10
    if size + 1e-9 < clip_shares:
        return ()
    return (Quote(True, price_up, size), Quote(False, price_down, size))
