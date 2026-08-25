"""Immutable strategy board shared by paper execution and replay."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
from collections.abc import Sequence

from paper.pair_types import PairConfig


MODEL_IDENTITY_SCHEMA = "project-fail-paper-model-v1"


def current_strategy_board(action_latency_s: float) -> tuple[PairConfig, ...]:
    """Return the exact focused paper board for a modeled action latency."""
    return (
        PairConfig(
            "basket99", "accumulate", 0.02,
            action_latency_s=action_latency_s, buy_sum_ceiling=0.99,
            improve_ticks=1, require_both_to_start=True,
            basket_average_cap=True, new_pair_start_s=30,
        ),
        PairConfig(
            "mintcycle5", "mint", 0.5,
            action_latency_s=action_latency_s, mint_sets=5,
            sell_sum_floor=1.005, new_pair_start_s=30,
            new_pair_cutoff_s=240, mint_anchor_spread=0.02,
        ),
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
    sources = sorted(
        (*root.joinpath("paper").glob("*.py"),
         root / "live" / "feed_health.py",
         root / "live" / "feed_pump.py",
         root / "live" / "mint_quotes.py",
         root / "live" / "window_clock.py",
         root / "tools" / "market_windows.py"),
        key=lambda path: path.relative_to(root).as_posix(),
    )
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
