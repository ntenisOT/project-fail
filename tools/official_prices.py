"""Fetch and cache official Chainlink opening/final TWAP values from Gamma."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from tools.market_windows import GAMMA, ResolvedWindow


@dataclasses.dataclass(frozen=True)
class OfficialPrice:
    slug: str
    price_to_beat: str
    final_price: str
    config_id: str
    lookback_s: int

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "OfficialPrice":
        row = cls(
            str(value["slug"]), str(value["price_to_beat"]),
            str(value["final_price"]), str(value["config_id"]),
            int(str(value["lookback_s"])),
        )
        row.validate()
        return row

    def validate(self) -> None:
        try:
            opening, final = Decimal(self.price_to_beat), Decimal(self.final_price)
        except InvalidOperation as exc:
            raise ValueError(f"invalid official price for {self.slug}") from exc
        if (not opening.is_finite() or not final.is_finite()
                or opening <= 0 or final <= 0 or self.lookback_s not in (30, 60)
                or not self.config_id.endswith(f"twap-{self.lookback_s}")):
            raise ValueError(f"invalid official TWAP metadata for {self.slug}")

    @property
    def winner_up(self) -> int:
        return int(Decimal(self.final_price) >= Decimal(self.price_to_beat))


def parse_gamma_price(window: ResolvedWindow, payload: object) -> OfficialPrice:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ValueError(f"Gamma event missing for {window.slug}")
    event = payload[0]
    metadata = event.get("eventMetadata")
    markets = event.get("markets")
    if not isinstance(metadata, dict) or not isinstance(markets, list):
        raise ValueError(f"official price metadata missing for {window.slug}")
    market = next(
        (row for row in markets
         if isinstance(row, dict) and row.get("slug") == window.slug),
        None,
    )
    if not isinstance(market, dict):
        raise ValueError(f"Gamma market missing for {window.slug}")
    config = market.get("cryptoMarketConfig")
    if not isinstance(config, dict) or config.get("twapEnabled") is not True:
        raise ValueError(f"TWAP config missing for {window.slug}")
    row = OfficialPrice(
        window.slug, str(metadata.get("priceToBeat")),
        str(metadata.get("finalPrice")),
        str(market.get("cryptoMarketConfigId") or config.get("id") or ""),
        int(str(config.get("twapLookbackSeconds"))),
    )
    row.validate()
    if row.winner_up != window.winner_up:
        raise ValueError(f"official prices contradict outcome for {window.slug}")
    return row


def fetch_gamma_price(window: ResolvedWindow, attempts: int = 4) -> OfficialPrice:
    request = urllib.request.Request(
        f"{GAMMA}/events?slug={window.slug}",
        headers={"User-Agent": "project-fail-forensics/1"},
    )
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response, parse_float=str)
            return parse_gamma_price(window, payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            error = exc
            if attempt + 1 < attempts:
                delay = (2 ** attempt if isinstance(exc, urllib.error.HTTPError)
                         and exc.code == 429 else 0.4)
                time.sleep(delay)
    raise RuntimeError(f"Gamma price fetch failed for {window.slug}: {error}")


class _RequestPacer:
    """Serialize request starts so a worker pool cannot burst Gamma."""

    def __init__(self, interval_s: float) -> None:
        self.interval_s = interval_s
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            if delay > 0:
                time.sleep(delay)
            self._next_at = time.monotonic() + self.interval_s


def load_price_cache(path: pathlib.Path) -> dict[str, OfficialPrice]:
    rows: dict[str, OfficialPrice] = {}
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                row = OfficialPrice.from_dict(json.loads(raw))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid price cache row {path}:{lineno}: {exc}") from exc
            previous = rows.get(row.slug)
            if previous is not None and previous != row:
                raise ValueError(f"conflicting official prices for {row.slug}")
            rows[row.slug] = row
    return rows


def _write_price_cache(path: pathlib.Path, rows: Mapping[str, OfficialPrice]) -> None:
    """Atomically checkpoint a complete, deterministic cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for slug in sorted(rows):
            handle.write(json.dumps(dataclasses.asdict(rows[slug]), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_price_errors(path: pathlib.Path, errors: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for slug in sorted(errors):
            handle.write(json.dumps({"slug": slug, "error": errors[slug]}, sort_keys=True)
                         + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def official_prices(
    windows: Sequence[ResolvedWindow], cache_path: str | pathlib.Path, *,
    workers: int = 2, fetch_missing: bool = True, allow_missing: bool = False,
    min_request_interval_s: float = 0.25, checkpoint_every: int = 25,
    errors_path: str | pathlib.Path | None = None,
) -> dict[str, OfficialPrice]:
    if workers <= 0 or min_request_interval_s < 0 or checkpoint_every <= 0:
        raise ValueError("workers/checkpoint interval must be positive")
    target = pathlib.Path(cache_path)
    cached = load_price_cache(target)
    missing = [window for window in windows if window.slug not in cached]
    errors: dict[str, str] = {}
    if missing and fetch_missing:
        pacer = _RequestPacer(min_request_interval_s)

        def fetch(window: ResolvedWindow) -> OfficialPrice:
            pacer.wait()
            return fetch_gamma_price(window)

        completed_since_checkpoint = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            jobs = {pool.submit(fetch, window): window for window in missing}
            for job in as_completed(jobs):
                window = jobs[job]
                try:
                    row = job.result()
                except RuntimeError as exc:
                    errors[window.slug] = str(exc)
                    continue
                cached[row.slug] = row
                completed_since_checkpoint += 1
                if completed_since_checkpoint >= checkpoint_every:
                    _write_price_cache(target, cached)
                    completed_since_checkpoint = 0
        _write_price_cache(target, cached)
    if fetch_missing:
        error_target = (pathlib.Path(errors_path) if errors_path is not None else
                        target.with_suffix(target.suffix + ".errors.jsonl"))
        _write_price_errors(error_target, errors)
    absent = [window.slug for window in windows if window.slug not in cached]
    if absent and not allow_missing:
        raise RuntimeError(f"{len(absent)} official price rows missing; first={absent[0]}")
    return {window.slug: cached[window.slug] for window in windows if window.slug in cached}
