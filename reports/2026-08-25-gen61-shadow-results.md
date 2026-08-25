# Gen61 official-TWAP shadow results

## Run identity

- Revision: `a18547bd8eb638a1cd42af424c9378d1cc01c419`
- Ireland paper runtime: 2026-08-25 14:59:22-15:26:xx UTC
- Scope: BTC five-minute markets, five unchanged strategy arms, 65 ms simulated
  action latency, 400 ms market-event freshness cutoff, no order placement
- DB: `out/gen61-final.db`
  - SHA-256: `659c0073c1a82da218e0aefc0d6627d6383b55cb1eb7df3416ab4feef8725988`
- Log: `out/gen61-run.log`
  - SHA-256: `af0c9c02df8bb7fa5a27ff7f422810e52fd9f433313aaa88a415c3323717a15d`
- Remote archive:
  `paper/paper_gen61_shadow_20260825T152659Z.{db,log}`

The SQLite archive passed `PRAGMA integrity_check`. It contains 95 fills, 15
invalid strategy-windows, 10 scored settlements, 10 metric rows, and 1,588
official reference ticks.

## Shadow observations

All four market openings were observed exactly. The T+30 sample is the newest
60-second TWAP update that was both observed and received by the decision time.

| Start | T+30 signal | Source age | Official winner | First mint5 sale | Mint5 completion | Execution validity |
|---|---:|---:|---|---|---|---|
| 15:00 | -1.39 bp | 3.0 s | Down | Up at $0.31 | 5+5 shares at $1.03 | invalid: reconnect |
| 15:05 | +1.49 bp | 3.0 s | Up | Down at $0.37 | 5+5 shares at $1.03 | invalid: reconnect |
| 15:10 | -0.12 bp | 2.0 s | Down | Down at $0.55 | incomplete: no Up sale | valid, lagged |
| 15:15 | -1.36 bp | 2.0 s | Up | Down at $0.76 | 5+5 shares at $1.03 | valid, clean for mint5 |

The signal sign matched three of four outcomes. That is not a usable hit-rate
estimate and does not justify a threshold. More importantly, signal correctness
did not guarantee safe inventory: the 15:10 sign was correct, but the first fill
sold the winning token and left five losing shares. At 15:15 the signal was
wrong, but both legs completed in 0.56 seconds and direction became irrelevant.

## Officially scored strategy evidence

Only 15:10 and 15:15 were execution-valid. Both were classified as lagged at the
whole-strategy level because a later source-event tail overlapped some exposure;
mintcycle5's 15:15 cycle itself was clean.

| Strategy | Windows | PnL | Neutral mechanics | Outcome component | Unmatched |
|---|---:|---:|---:|---:|---:|
| Basket99 | 2 | -$1.90 | +$0.60 | -$2.50 | 5.0 |
| Basket99c180 | 2 | -$1.90 | +$0.60 | -$2.50 | 5.0 |
| mintcycle5 | 2 | -$2.10 | +$0.40 | -$2.50 | 5.0 |
| mintcycle20 | 2 | -$3.26 | +$0.17 | -$3.44 | 6.9 |
| minthedge60p95 | 2 | -$3.26 | +$0.17 | -$3.44 | 6.9 |

The sole clean mintcycle5 window earned +$0.15 by selling a complete pair at
$1.03. The incomplete window lost $2.25. If successful cycles continue to earn
$0.15 and incomplete cycles are toxic at -$2.25, completion must exceed 93.75%
before fees and uncredited rebates merely to break even. The four observed
mintcycle5 mechanisms completed 3/4, while only two were execution-valid. This
is nowhere near sufficient evidence of winner-level completion or profit.

Maker markouts reinforce the problem. Across the two scored windows,
mintcycle5 was +0.50 cents at one second and +1.17 cents at five seconds, but
-3.50 cents at 15 seconds. Basket99 was -1.50, -6.42, and -15.36 cents. A small
initial spread advantage does not survive selected residual inventory.

## Latency and transport

- Normal public event age was about 9-13 ms at p50. Local ordered-pump residence
  stayed at or below 13 ms, with high-water 113 against capacity 8,192.
- Source-event tails reached 3.957 seconds. These are intermittent tails, not a
  one-second steady-state latency estimate.
- The market socket received four `1013 slow consumer: send buffer full`
  closures at 15:02:51, 15:03:42, 15:06:03, and 15:21:20 UTC. Reconnects took
  about 0.17 seconds and invalidated the affected active strategy window.
- The independent Chainlink stream recorded 1,588 ticks with zero reconnects;
  its maximum observed publication/receipt age was about 3.2 seconds.

A same-host, same-token bare receiver was run with the same WebSocket queue
limit and no scoring work:

| Probe | Duration | Events | Rate | JSON parse p90 | Max receive gap | 1013 |
|---|---:|---:|---:|---:|---:|---|
| late 15:05 window | 71.5 s | 13,686 | 191/s | 0.032 ms | 148 ms | no |
| 15:10 overlap | 253.5 s | 107,236 | 423/s | 0.029 ms | 1,716 ms | no |

The full runner also remained connected throughout the exact second probe.
Therefore this rules out sustained JSON parsing pressure but does not localize
the intermittent 1013. A longer matched full-versus-bare exposure is required.

## Reporting defect found and corrected for Gen62

Gen61 persisted the official winner only inside valid per-strategy settlement
rows. Reconnect-invalidated windows retained their reference ticks but lost the
market outcome needed by the shadow audit. This coupled signal measurement to
execution health and reduced the built-in report from four resolved signals to
two.

The Gen62 candidate adds one market-level `resolved_windows` row before any
strategy scoring and makes the reference audit prefer it while retaining legacy
settlement fallback. It changes no quote, fill, latency, or order behavior.

## Decision and next ideas

1. Deploy only the market-level outcome persistence correction and rerun the
   unchanged shadow board. Do not add a signal gate.
2. Require a materially larger, time-ordered sample before estimating a signal
   threshold. Report accuracy, calibration by absolute basis-point bucket, and
   the lower confidence bound of net PnL—not raw hit rate.
3. Treat completion as the mint strategy's primary optimization target. Measure
   the conditional completion probability and expected residual loss after the
   first leg by price, time, queue age, TWAP regime, and executable opposite-leg
   pair sum.
4. Do not blindly lower the hedge floor from one toxic window. First measure the
   counterfactual cost of immediate completion. A balancer is useful only if its
   saved tail loss exceeds its paid spread and taker fee out of sample.
5. Run a longer matched bare/full transport probe with frame rate, parse time,
   event-loop delay, and 1013 incidence. Keep it non-scoring and independent of
   strategy behavior.
6. After observer acceptance, remove redundant Basket99c180, mintcycle20, and
   minthedge60p95 arms. Keep mintcycle5 as the smallest falsification control and
   use the freed board capacity for one evidence-backed completion policy.

Real-money execution remains **NO-GO**. No live order was placed in Gen61.
