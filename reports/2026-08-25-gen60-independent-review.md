# Generation 60 independent review — 2026-08-25

## Decision

**NO-GO. Do not deploy a signal, change quote parameters, or use live money.**
Generation 60 rejected more mint tuning and produced one useful next step:
collect the correct official 60-second Chainlink TWAP as a causal shadow signal.
That observer remains local and undeployed pending user approval.

## Generation 60 result

Three officially scored BTC windows, one WebSocket-reconnect window invalidated,
and one startup window invalidated. The original Ireland process stopped
gracefully after the 14:00 window resolved.

| Strategy | PnL | Pair edge | Neutral | Direction | Worst | Unmatched | Markout 1/5/15s |
|---|---:|---:|---:|---:|---:|---:|---:|
| basket99 | -$0.72 | +$1.91 | +$4.46 | -$5.18 | -$0.72 | 10.4 | +0.16c / -1.11c / +2.01c |
| basket99c180 | -$0.72 | +$1.91 | +$4.46 | -$5.18 | -$0.72 | 10.4 | +0.16c / -1.11c / +2.01c |
| mintcycle20 | -$1.35 | +$1.15 | +$1.15 | -$2.50 | -$1.35 | 5.0 | -1.50c / +0.74c / -1.53c |
| minthedge60p95 | -$1.57 | +$0.93 | +$0.93 | -$2.50 | -$1.57 | 5.0 | -1.38c / +0.56c / -1.62c |
| mintcycle5 | -$2.20 | +$0.30 | +$0.30 | -$2.50 | -$2.20 | 5.0 | -1.50c / +0.30c / -1.90c |

The official outcome was the adverse side of Basket99's residual in all three
windows. That is why realized PnL equaled the adverse floor; it is not an
accounting convention. Three windows cannot establish a rate, but the pattern is
consistent with selected residual rather than zero-mean settlement luck.

The critical mint event occurred in the 13:50 window. Every mint arm sold five
Down shares at 0.50 after only 0.489 seconds of order residence. No Up leg
filled, Down won, and each arm lost $2.50. The fill's signed markout was -2.5c
at one second, -3.5c at five seconds, and -13.5c at fifteen seconds. A 916 ms
feed tail occurred around T+280, long after the fill and markout horizons. It
correctly labels later open exposure as lagged but cannot explain the immediate
pick-off. Faster repricing is therefore not the next justified fix.

Across Generations 59 and 60, with the same trading behavior:

| Strategy | Combined PnL | Combined neutral |
|---|---:|---:|
| basket99 | +$1.29 | +$7.46 |
| mintcycle5 | -$3.90 | +$1.10 |
| mintcycle20 | -$5.20 | +$2.30 |
| minthedge60p95 | -$5.85 | +$1.65 |

These are six tiny, feed-tail-exposed windows, not edge estimates. They do show
that positive paired spread has repeatedly failed to cover selected residual.

## Winner evidence

The old mint-and-ask winner story is invalid. Correct CLOB V2 normalization
proves both-token maker acquisition, not direct CTF minting or simultaneous asks.

Historical clean wallet `0xb27...`:

- 1,081,112 completed FIFO pair-shares across 253 BTC markets, about 4,273 per
  market.
- 97.3% acquisition completion, 98.2% maker/maker, average pair $0.982.
- FIFO completion d50 8 seconds and d90 45 seconds.
- Only 61.3% of individual pairs cost at most $1.00; cheap pairs subsidized
  later balancing above $1.00.

Fresh cohort for the same wallet:

- 96.6% completion and 99.3% maker flow persisted.
- Average pair worsened to $1.015 and terminal edge was -$4,292.

The decisive conclusion is that winner-shaped mechanics are not edge. Basket99
is closer to the proven paired-bid mechanism than any mint arm, but our one
five-share order per side is not a scaled-down version of a broad, replenished
thousands-of-pairs ladder. The mint arms are counter-evidenced by the corrected
forensics and should be treated only as falsification controls.

## Independent reviews

### Qwen 3.8 Max

Exact `qwen3.8-max` returned a complete nine-part review: 89,034 tokens, no file
changes. Its main conclusions:

- The deepest remaining mistake is conflating mechanism with edge.
- Gen60 captured one real toxic event, not a statistically estimated adverse
  selection rate.
- Deploy only the 60-second TWAP observer in Gen61; no quote gate or threshold.
- Use time-ordered train/calibration/untouched-test cohorts, account for serial
  dependence with block bootstrap, and require a positive lower confidence bound
  on net improvement after sacrificed pair opportunities.
- Diagnose 1013 with an isolated bare receiver versus the full pipeline while
  measuring socket receipt, parsing, enqueue time, event-loop lag, and boundary
  bursts.
- Retire the duplicate c180 control and the larger mint/hedge arms after the
  instrumentation-only generation; retain mintcycle5 as a minimal control.

Qwen raised three concrete implementation concerns. Code audit resolved two:
E18 is already stored as TEXT and tested with a realistic 6.5e22 value, and
`received_at` is stamped directly after socket receipt before the writer queue.
Its exact-start coverage/miss-rate concern remains valid and should become an
explicit observer acceptance metric.

Qwen also called lagged-window scoring inconsistent with fail-closed handling.
The packet's wording was too broad. The implementation deliberately freezes new
decisions but scores exchange exposure that already existed, stratifying it as
lagged; a hard reconnect invalidates the window. Dropping existing exposure on a
feed tail would create survivorship bias.

### Claude Opus 5 Max

The first exact `claude-opus-5` run used max effort, standard service, and
reported `fast_mode_state=off`. It returned only sections 1–3 of 9, then stopped
as a plan-style response. A corrected direct-answer retry produced no output in
fifteen minutes and was terminated. It does not count as a full review.

The partial response made two material accounting errors: it treated official
realized PnL as a fixed adverse mark and divided the explicitly defined complete
pair-share count by two. Those conclusions are rejected.

One partial Opus insight survives audit: the historical wallet accepts many
balancing pairs above $1.00, while our hard hold-out policy keeps completed-pair
cost attractive partly by leaving selected inventory unmatched. That does not
justify blindly paying up—the fresh wallet cohort lost at $1.015—but it identifies
the correct future design problem: dynamic balancing under a regime gate, not a
hard cap on every pair.

## Latency and feed

- A blanket one-second latency assumption was wrong. Healthy public CLOB event
  age was about 9–10 ms median; simulated action activation averaged 72–73 ms.
- The configured 65 ms action delay remains a sensitivity assumption. No
  authenticated POST/cancel distribution has been measured.
- Upstream tails reached 3.656 seconds and a two-token feed received 1013
  `slow consumer: send buffer full` after processing more than 1.2 million
  `price_change` events in about thirty minutes.
- Local pump residence stayed at or below 5 ms in the disconnect window. This
  reduces the likelihood that the strategy loop was the direct bottleneck but
  does not rule out socket-read, parsing, event-loop, OS-buffer, or upstream
  shedding problems.

## Correct reference signal

Current five-minute crypto markets use the official 60-second Chainlink TWAP for
opening and settlement. The previous spot/legacy Chainlink probe measured the
wrong reference.

The local, undeployed candidate:

- Subscribes to `crypto_prices_twap_sixty` with the exact BTC filter.
- Stores exact E18 as TEXT with observation and local receive timestamps.
- Ignores empty control frames and sends the documented text `PING` every five
  seconds.
- Reconstructs the exact opening sample and the latest causally received sample
  at T+30; missing data fails closed.
- Does not influence orders.
- Passes 68 tests plus focused Ruff, mypy, and compilation.

No useful signal-strength number is known. The old roughly five-cents-per-share
gross threshold is only an economic screen near 0.50 after taker fee, execution,
and safety allowance. It cannot be translated into TWAP basis points without
data. A future gate must satisfy:

`lower_95(net improvement after fees, adverse selection, and sacrificed pairs) > 0`

on an untouched time-ordered cohort. The initial roughly 250-window cohort can
test collector viability and produce an event-rate estimate; it is not enough by
itself if toxic opening fills are rare.

## Proposed next iterations — not executed

### Generation 61: one change

Deploy the shadow-only official TWAP observer. Keep trading behavior unchanged.

Engineering acceptance proposal:

- Exact opening plus causal T+30 signal on at least 90% of otherwise-valid
  windows; report every miss and nearest observed timestamp. This 90% figure is
  an engineering availability gate, not a statistical threshold.
- No reference-writer queue failure; E18 round-trip remains exact.
- CLOB pump residence remains within the Gen60 envelope and no reconnect is
  attributable to the reference task.
- Reference age, reconnects, and gaps are reported separately.
- Zero code path from the shadow signal to quoting.

### Generation 62

Split opening-leg and completing-leg markouts. Aggregate markouts currently mix
the liability-creating event with the balancing event.

### Separate bounded transport probe

Run a non-scoring bare two-token receiver beside the full pipeline and compare
1013 incidence, message rate, parse/enqueue time, event-loop lag, and market
boundary phase. This separates upstream shedding from client processing without
changing strategy behavior.

### Board pruning after observer acceptance

- Remove `basket99c180` unless it has a genuinely different hypothesis; Gen60
  was byte-for-byte identical in behavior and outcome.
- Retire `mintcycle20` and `minthedge60p95` from the focused board. They add
  repeated versions of the same rejected tail. Keep `mintcycle5` as the smallest
  falsification control.
- Do not lower the hedge floor, tighten hysteresis, or promote a one-pair cap.
- Keep a future ladder/balancer design on paper until the TWAP/regime study
  demonstrates a causal exclusion rule.

## Artifacts

- Evidence packet: `out/gen60-review-packet.md`
- Final database: `out/gen60-final.db`
- Runtime log: `out/gen60-run.log`
- Candidate code is uncommitted and undeployed.
