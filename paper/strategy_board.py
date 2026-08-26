"""Immutable strategy board shared by paper execution and replay."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
from collections.abc import Sequence

from paper.pair_types import PairConfig


MODEL_IDENTITY_SCHEMA = "project-fail-paper-model-v2"
MODEL_SOURCES = (
    "paper/buy_completion.py",
    "paper/capture.py",
    "paper/cohort_engine.py",
    "paper/exposure.py",
    "paper/fill_probe.py",
    "paper/ladder_engine.py",
    "paper/ledger.py",
    "paper/ledger_writer.py",
    "paper/market_metadata.py",
    "paper/order_book.py",
    "paper/pair_engine.py",
    "paper/pair_lots.py",
    "paper/pair_types.py",
    "paper/reference_feed.py",
    "paper/reference_view.py",
    "paper/run.py",
    "paper/settlement.py",
    "paper/strategy_board.py",
    "paper/taker.py",
    "paper/twap_engine.py",
    "live/feed_health.py",
    "live/feed_pump.py",
    "live/loop_health.py",
    "live/mint_quotes.py",
    "live/window_clock.py",
    "tools/market_windows.py",
    "tools/transport_telemetry.py",
)


def current_strategy_board(action_latency_s: float) -> tuple[PairConfig, ...]:
    """Return the exact focused paper board for a modeled action latency."""
    common = dict(
        action_latency_s=action_latency_s, improve_ticks=1,
        require_both_to_start=True, basket_average_cap=True, new_pair_start_s=30,
    )
    # Gen94 rejected the residual-flatten timings and the T+240 TWAP arm. Its
    # four-asset feed was also operationally invalid (repeated provider 1013s),
    # so the next board is a BTC-only observational capture with one untouched
    # control.
    #
    # Exact Aug18-25 wallet lifecycles show that 0x75/0x5e/0x20d are takers, not
    # paired maker arbitrageurs. Their selected buys earn about +0.3c to +1.3c
    # per action, but the very next public ask is 1.5c to 2.3c worse and flips
    # the same selection negative. Polygon settlement times cannot identify
    # their offchain trigger, so a prospective causal book test is required.
    #
    # terminal10 was implemented as the smallest honest copy attempt: frozen
    # 10c/10s momentum, 65ms local action time plus the documented 250ms taker
    # delay, one-tick chase cap, fee, and terminal hold. A causal Gen94
    # counterfactual filled all 16 clean BTC opportunities and lost $8.44
    # (neutral -$13.44; adverse floor -$38.44), corroborating the independent
    # historical holdout failure. Keep its engine for prospective/offline
    # falsification, but do not put a rejected arm on the active board.
    return (
        PairConfig("basket99", "accumulate", 0.02, buy_sum_ceiling=0.99, **common),
    )


def preopen_strategy_board(action_latency_s: float) -> tuple[PairConfig, ...]:
    """Frozen first test of the already-live upcoming-window order book.

    The two arms differ only in queue retention.  Both place minimum-size
    maker bids whose combined price is at most 99 cents, beginning four
    minutes before the price-measurement interval.  ``pre99_hold`` retains a
    balanced pair through T+15 unless a leg fills; ``pre99_dynamic`` is the
    old book-following behavior and is the contemporaneous control.
    """
    common = dict(
        action_latency_s=action_latency_s,
        improve_ticks=0,
        require_both_to_start=True,
        basket_average_cap=True,
        new_pair_start_s=-240,
        buy_sum_ceiling=0.99,
        clip_shares=5,
        max_inventory=20,
    )
    return (
        PairConfig("pre99_hold", "accumulate", 0.02, quote_hold_s=255, **common),
        PairConfig("pre99_dynamic", "accumulate", 0.02, **common),
    )

def canonical_board(configs: Sequence[PairConfig]) -> str:
    """Serialize every config field deterministically for experiment provenance."""
    return json.dumps(
        [dataclasses.asdict(config) for config in configs],
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def strategy_board_hash(configs: Sequence[PairConfig]) -> str:
    """Return the SHA-256 identity of a canonical strategy board."""
    return hashlib.sha256(canonical_board(configs).encode("utf-8")).hexdigest()


def execution_model_identity() -> dict[str, object]:
    """Hash the exact checked-out source that defines paper economics and timing."""
    root = pathlib.Path(__file__).resolve().parents[1]
    sources = [root / relative for relative in MODEL_SOURCES]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise RuntimeError(f"paper model source is missing: {missing}")
    digest = hashlib.sha256()
    for path in sources:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return {
        "schema": MODEL_IDENTITY_SCHEMA,
        "sha256": digest.hexdigest(),
        "source_count": len(sources),
    }
