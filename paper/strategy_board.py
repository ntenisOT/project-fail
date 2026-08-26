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
    "paper/run.py",
    "paper/settlement.py",
    "paper/strategy_board.py",
    "paper/taker.py",
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
    mint = dict(
        action_latency_s=action_latency_s, mint_anchor_spread=0.0,
        clip_shares=6.0, mint_sets=200.0, max_inventory=200.0,
        imbalance_tolerance=7.0, require_both_to_start=True,
    )
    return (
        # Incumbent control: buy-side paired accumulator (the retired thesis).
        PairConfig(
            "basket99", "accumulate", 0.02,
            action_latency_s=action_latency_s, buy_sum_ceiling=0.99,
            improve_ticks=1, require_both_to_start=True,
            basket_average_cap=True, new_pair_start_s=30,
        ),
        # PUBLIC-TAPE FINDING (600 BTC windows, 3.9M trades): the traded pair
        # cost rises monotonically through the window --
        #   -180..0s 0.998 | 0..120s ~1.00 | 120..180s 1.02
        #   180..240s 1.04-1.07 | 240..300s 1.08-1.11
        # So a minted set is worth ~par early and an 8-11% premium late. Early
        # mint arms could never fill because a >=1.00 ask sat ABOVE a 0.998
        # market. These arms concentrate selling where the premium exists.
        PairConfig("mintlate", "mint", 0.02, sell_sum_floor=1.03,
                   new_pair_start_s=180, new_pair_cutoff_s=295, **mint),
        # Higher-premium twin: only sells into the richest part of the curve.
        PairConfig("mintlate_f6", "mint", 0.02, sell_sum_floor=1.06,
                   new_pair_start_s=210, new_pair_cutoff_s=295, **mint),
        # Retained early control: proves the zero-fill diagnosis is timing, not
        # tolerance or floor (this is the previous mintwin configuration).
        PairConfig("mintwin", "mint", 0.02, sell_sum_floor=1.00,
                   new_pair_start_s=5, new_pair_cutoff_s=285, **mint),
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
