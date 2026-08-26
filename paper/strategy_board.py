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
    return (
        # Incumbent control, unchanged so its history stays comparable.
        PairConfig("basket99", "accumulate", 0.02, buy_sum_ceiling=0.99, **common),
        # SELECTIVITY TEST. Out-of-sample persistence (tools/winner_persistence.py,
        # 78 wallets, Aug18-22 vs Aug22-25): margin persists (rho +0.517, z +4.54)
        # and the behaviour predicting HIGHER next-period margin is selective and
        # thin, not continuous:
        #   both-sided %  -0.464 (z -4.07)   fills/market -0.361 (z -3.16)
        #   volume        -0.363 (z -3.18)   maker share  -0.230 (z -2.02)
        # Every mint arm was the opposite profile and took zero fills. These arms
        # run the same mechanic as basket99 but demand a cheaper pair, so they
        # fill less often at a better price: selectivity on the price axis, which
        # the engine supports without a refactor. The tape shows the cheapest 25%
        # of pre-open volume reaches <=0.97 in 58% of windows.
        PairConfig("basket97", "accumulate", 0.02, buy_sum_ceiling=0.97,
                   patient_bids=True, **common),
        PairConfig("basket95", "accumulate", 0.02, buy_sum_ceiling=0.95,
                   patient_bids=True, **common),
        # Momentum RETIRED at Gen86/38 windows. The signal is real but ~5x too
        # small to pay for itself: mom10 earned +$5.18 gross against $26.93 of
        # taker fees (+$0.14/window of edge versus $0.71/window of cost), and
        # mom05 was negative even gross (-$17.53), exactly as the tape said a
        # 5c threshold would be. tools/momentum_probe.py measured +0.0196/share
        # net, but it priced fills at 10s bucket VWAPs while the live engine
        # sweeps real displayed depth across levels - that optimism was worth
        # the entire edge. Taking liquidity on this signal cannot work.
        #
        # basket ceilings run monotonic at 38 windows (0.99 +12.7% > 0.97 +0.3%
        # > 0.95 -11.3%) and basket99 achieves a 0.933 average pair, well inside
        # its own ceiling - so the ceiling is not what binds. basket100 tests
        # whether looser still is better, or whether the trend breaks once the
        # cap stops excluding anything.
        PairConfig("basket100", "accumulate", 0.02, buy_sum_ceiling=1.00,
                   patient_bids=True, **common),
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
