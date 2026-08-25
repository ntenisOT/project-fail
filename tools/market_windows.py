"""Resolve and cache official 5-minute crypto market metadata."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Mapping, Sequence


ASSET_PREFIX = {
    "btc": "btc-updown-5m",
    "eth": "eth-updown-5m",
    "sol": "sol-updown-5m",
    "xrp": "xrp-updown-5m",
}
GAMMA = "https://gamma-api.polymarket.com"
TOKEN_RE = re.compile(r"^[0-9]+$")
CONDITION_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


@dataclasses.dataclass(frozen=True)
class ResolvedWindow:
    slug: str
    asset: str
    start: int
    condition_id: str
    up_token: str
    down_token: str
    winner_up: int

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ResolvedWindow":
        item = cls(
            slug=str(value["slug"]),
            asset=str(value["asset"]).lower(),
            start=int(str(value["start"])),
            condition_id=str(value["condition_id"]),
            up_token=str(value["up_token"]),
            down_token=str(value["down_token"]),
            winner_up=int(str(value["winner_up"])),
        )
        item.validate()
        return item

    def validate(self) -> None:
        prefix = ASSET_PREFIX.get(self.asset)
        if prefix is None or self.slug != f"{prefix}-{self.start}":
            raise ValueError(f"invalid asset/slug/start mapping: {self.slug!r}")
        if self.start % 300 or self.winner_up not in (0, 1):
            raise ValueError(f"invalid timing/outcome in {self.slug}")
        if not TOKEN_RE.fullmatch(self.up_token) or not TOKEN_RE.fullmatch(self.down_token):
            raise ValueError(f"invalid token id in {self.slug}")
        if self.up_token == self.down_token or not CONDITION_RE.fullmatch(self.condition_id):
            raise ValueError(f"invalid token/condition mapping in {self.slug}")


def _json_list(value: object, field: str) -> list[object]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError(f"Gamma {field} is not a list")
    return value


def parse_gamma_event(asset: str, start: int, payload: object) -> ResolvedWindow | None:
    """Parse one Gamma response, returning None while unresolved."""
    slug = f"{ASSET_PREFIX[asset]}-{start}"
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None
    markets = payload[0].get("markets") or []
    if not isinstance(markets, list) or not markets:
        return None
    market = next(
        (m for m in markets if isinstance(m, dict) and m.get("slug") == slug),
        None,
    )
    if market is None:
        return None
    if not isinstance(market, dict):
        raise ValueError(f"Gamma market for {slug} is malformed")

    tokens = [str(v) for v in _json_list(market.get("clobTokenIds"), "clobTokenIds")]
    outcomes = [str(v).strip().lower() for v in _json_list(market.get("outcomes"), "outcomes")]
    prices = [float(str(v)) for v in _json_list(market.get("outcomePrices"), "outcomePrices")]
    if len(tokens) != len(outcomes) or len(prices) != len(outcomes):
        raise ValueError(f"Gamma token/outcome/price lengths differ for {slug}")
    try:
        up_i, down_i = outcomes.index("up"), outcomes.index("down")
    except ValueError as exc:
        raise ValueError(f"Gamma outcomes are not explicit Up/Down for {slug}") from exc
    if not market.get("closed") or max(prices) < 0.999 or min(prices) > 0.001:
        return None
    winner_i = max(range(len(prices)), key=prices.__getitem__)
    return ResolvedWindow(
        slug=slug,
        asset=asset,
        start=start,
        condition_id=str(market.get("conditionId") or market.get("condition_id") or ""),
        up_token=tokens[up_i],
        down_token=tokens[down_i],
        winner_up=int(winner_i == up_i),
    )


def fetch_gamma_window(asset: str, start: int, attempts: int = 4) -> ResolvedWindow | None:
    slug = f"{ASSET_PREFIX[asset]}-{start}"
    request = urllib.request.Request(
        f"{GAMMA}/events?slug={slug}", headers={"User-Agent": "project-fail-forensics/1"}
    )
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return parse_gamma_event(asset, start, json.load(response))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            error = exc
            if attempt + 1 < attempts:
                delay = 2 ** attempt if isinstance(exc, urllib.error.HTTPError) and exc.code == 429 else 0.4
                time.sleep(delay)
    raise RuntimeError(f"Gamma fetch failed for {slug}: {error}")


def load_window_cache(path: os.PathLike[str] | str) -> dict[str, ResolvedWindow]:
    cached: dict[str, ResolvedWindow] = {}
    source = pathlib.Path(path)
    if not source.exists():
        return cached
    with source.open(encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                window = ResolvedWindow.from_dict(json.loads(raw))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid cache row {source}:{lineno}: {exc}") from exc
            previous = cached.get(window.slug)
            if previous is not None and previous != window:
                raise ValueError(f"conflicting cached resolution for {window.slug}")
            cached[window.slug] = window
    return cached


def resolve_windows(
    assets: Sequence[str],
    start: int,
    end: int,
    cache_path: os.PathLike[str] | str,
    workers: int = 8,
    fetch_missing: bool = True,
    allow_missing: bool = False,
) -> tuple[list[ResolvedWindow], list[str]]:
    cache = load_window_cache(cache_path)
    wanted = [(asset, ts) for asset in assets for ts in range(start, end + 1, 300)]
    absent = [(a, ts) for a, ts in wanted if f"{ASSET_PREFIX[a]}-{ts}" not in cache]
    fetched: list[ResolvedWindow] = []
    missing: list[str] = []
    if absent and fetch_missing:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            jobs = {pool.submit(fetch_gamma_window, a, ts): (a, ts) for a, ts in absent}
            for job in as_completed(jobs):
                asset, ts = jobs[job]
                slug = f"{ASSET_PREFIX[asset]}-{ts}"
                try:
                    window = job.result()
                except RuntimeError as exc:
                    missing.append(f"{slug}: {exc}")
                    continue
                if window is None:
                    missing.append(f"{slug}: unresolved")
                else:
                    fetched.append(window)
                    cache[slug] = window
        if fetched:
            target = pathlib.Path(cache_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                for window in sorted(fetched, key=lambda w: (w.start, w.asset)):
                    handle.write(json.dumps(dataclasses.asdict(window), sort_keys=True) + "\n")
    elif absent:
        missing.extend(f"{ASSET_PREFIX[a]}-{ts}: not cached" for a, ts in absent)

    resolved = [cache[f"{ASSET_PREFIX[a]}-{ts}"] for a, ts in wanted
                if f"{ASSET_PREFIX[a]}-{ts}" in cache]
    if missing and not allow_missing:
        examples = "\n  ".join(missing[:8])
        raise RuntimeError(
            f"{len(missing)} windows missing/unresolved; refusing a partial leaderboard.\n"
            f"  {examples}\nUse --allow-missing only intentionally."
        )
    return sorted(resolved, key=lambda w: (w.start, w.asset)), missing
