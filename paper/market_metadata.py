"""Fail-closed active-market discovery for the focused paper runner."""

from __future__ import annotations

import dataclasses
import json
import time
import urllib.request

from tools.market_windows import ASSET_PREFIX, CONDITION_RE, GAMMA, TOKEN_RE


@dataclasses.dataclass(frozen=True)
class ActiveMarket:
    asset: str
    slug: str
    start: int
    condition_id: str
    up_token: str
    down_token: str
    min_order_size: float


def _list(value: object) -> list[object]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("Gamma field is not a list")
    return value


def parse_active_market(asset: str, start: int, payload: object) -> ActiveMarket | None:
    slug = f"{ASSET_PREFIX[asset]}-{start}"
    if not isinstance(payload, list):
        return None
    markets = [market for event in payload if isinstance(event, dict)
               for market in (event.get("markets") or []) if isinstance(market, dict)]
    market = next((item for item in markets if item.get("slug") == slug), None)
    if market is None:
        return None
    outcomes = [str(value).strip().lower() for value in _list(market.get("outcomes"))]
    tokens = [str(value) for value in _list(market.get("clobTokenIds"))]
    if len(outcomes) != len(tokens):
        raise ValueError(f"Gamma outcome/token mismatch for {slug}")
    try:
        up_token, down_token = tokens[outcomes.index("up")], tokens[outcomes.index("down")]
    except ValueError as exc:
        raise ValueError(f"Gamma outcomes are not explicit Up/Down for {slug}") from exc
    condition = str(market.get("conditionId") or "")
    try:
        min_order_size = float(str(market.get("orderMinSize") or ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Gamma order minimum for {slug}") from exc
    if (not TOKEN_RE.fullmatch(up_token) or not TOKEN_RE.fullmatch(down_token)
            or up_token == down_token or not CONDITION_RE.fullmatch(condition)
            or min_order_size <= 0):
        raise ValueError(f"invalid Gamma identifiers for {slug}")
    return ActiveMarket(
        asset, slug, start, condition, up_token, down_token, min_order_size,
    )


def fetch_active_market(asset: str, start: int, attempts: int = 3) -> ActiveMarket | None:
    slug = f"{ASSET_PREFIX[asset]}-{start}"
    request = urllib.request.Request(
        f"{GAMMA}/events?slug={slug}", headers={"User-Agent": "project-fail-paper/2"}
    )
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                return parse_active_market(asset, start, json.load(response))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            error = exc
            if attempt + 1 < attempts:
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"Gamma fetch failed for {slug}: {error}")
