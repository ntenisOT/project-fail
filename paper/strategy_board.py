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
    common = dict(
        action_latency_s=action_latency_s, improve_ticks=1,
        require_both_to_start=True, basket_average_cap=True, new_pair_start_s=30,
    )
    # RESIDUAL FLATTEN TEST. Gen91 (11 windows, 91.7% validity) separated the
    # two halves of this strategy for the first time:
    #
    #   arm         edge$   outcome$   pnl$
    #   basket99    +3.86     -7.21   -1.72
    #   basket97    +4.82    -13.04   -5.36
    #   basket95    +3.09    -16.52   -9.69
    #   basket100   +3.70    -11.48   -4.09
    #
    # The FIFO-paired mechanic earns money on every arm; the naked leftover leg
    # loses more than it earns on every arm. Four independent arms do not all
    # lose a coin flip at once, so the residual is adversely selected: the leg
    # that fills and never completes is the leg informed takers were exiting.
    #
    # Ledger analysis of where the surviving imbalance is created rules out the
    # obvious fix. Only 10% of residual shares appear after T+240 and 60% before
    # T+180, so refusing late pair opens (new_pair_cutoff_s) would barely touch
    # it. The residual forms mid-window and simply never completes.
    #
    # So the arms below hold every other variable fixed at basket99's incumbent
    # settings and vary ONE thing: when we sell the naked excess into displayed
    # depth, paying a real taker fee. That converts an uncontrolled adverse coin
    # flip into a measured cost. basket99 stays as the untouched control so its
    # history remains comparable, and three timings give a dose-response rather
    # than a single yes/no.
    #
    # Prior arms retired here, not deleted from history: basket97/95 tested
    # selectivity on the price axis and basket100 tested a looser ceiling. All
    # three lost in Gen91, and their justification cited a winner-persistence
    # result that has since been retracted as future-selected (see README).
    # Momentum was retired at Gen86: real signal, ~5x too small for its fees.
    return (
        PairConfig("basket99", "accumulate", 0.02, buy_sum_ceiling=0.99, **common),
        PairConfig("basket99f285", "accumulate", 0.02, buy_sum_ceiling=0.99,
                   flatten_residual_s=285.0, **common),
        PairConfig("basket99f240", "accumulate", 0.02, buy_sum_ceiling=0.99,
                   flatten_residual_s=240.0, **common),
        PairConfig("basket99f180", "accumulate", 0.02, buy_sum_ceiling=0.99,
                   flatten_residual_s=180.0, **common),
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
