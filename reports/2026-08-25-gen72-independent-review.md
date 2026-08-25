# Independent review after Generation 72 — 2026-08-25

## Bottom line

**NO-GO for live money and NO-GO for another tiny strategy generation.** The
recent cadence mixed engineering smoke tests, transport diagnostics, historical
forensics, and economic experiments under one sequence of generation numbers.
That created visible activity but far less strategy evidence than the labels
implied.

The real progress is narrower:

1. the public-feed healthy path is fast, not one second;
2. the intermittent 1013 disconnect is real and not explained by measured
   application/client queue saturation;
3. bounded fee-aware completion reduces one-sided inventory risk in the current
   paper model;
4. the existing mint strategy is economically falsified under that same model;
5. current winner evidence shows several distinct lifecycle strategies, not one
   universal mint-and-ask bot;
6. neither profitability nor a signal threshold has been established.

The next work should be a predeclared transport experiment and a deeper winner/
loser lifecycle study, run in parallel. Strategy tuning should resume only if
those results preserve the paired-accumulation premise and the fill model is
hardened.

## What each recent iteration actually tested

These were mostly short 2-4-valid-window probes. They are relabeled honestly
below; they should not be read as thirteen independent strategy experiments.

| Generation | Actual question | Class | Result |
|---|---|---|---|
| 60 | Are the then-current basket/mint arms economically credible, and is the reference signal correct? | diagnosis | Mint tail dominated; old reference was wrong; official 60 s TWAP observer proposed. |
| 61 | Can the official TWAP be collected causally without changing orders? | instrumentation smoke | Observer worked; four 1013 closes; outcome persistence defect found. |
| 62 | Does market-level outcome persistence fix the shadow audit, and what do fresh winners look like? | instrumentation + forensics | Ledger fixed; at least neutral-pair and directional winner regimes found. |
| 63 | Can bounded T+270 basket completion and p95 mint repair reduce residual risk? | mechanics smoke | One useful basket completion; p95 did not execute; larger mint rejected. |
| 64 | Can near-minimum dust rounding complete a basket, and is p95 mint repair useful? | mechanics smoke | Dust arm did not trigger correctly at capacity; p95 execution paid too much. |
| 65 | Does corrected dust allowance work, and how do p95/p100 repairs trade edge for insurance? | mechanics smoke | One winner-like rolling-basket completion; settlement bug fixed; p95 outcomes conflicted. |
| 66 | Does the Gen65 fix persist safely, and are feed tails local saturation? | recovery smoke | Very small valid cohort; another 1013; no strategy conclusion. |
| 67 | Do parse time/event-loop/application residence explain 1013? | transport instrumentation | No: 4.493 s source tail with ~2 ms local timings. |
| 68 | Is a 50-share cap binding, and does reconnect still occur? | mechanics + transport smoke | x50 identical; two 1013 closes; capacity hypothesis rejected. |
| 69 | Will planned boundary refresh prevent 1013? | transport experiment | No: fresh sockets closed 45 s and 130 s after refresh. |
| 70 | Is the internal 1024-frame client queue saturating? | transport experiment | No: HWM 145; 4096 increase could not be credited. Timed completion looked safer in three lagged windows. |
| 71 | Will <=0.1-share dust tolerance restart pair churn? | mechanics smoke | No: paid more for the same paired shares and added unmatched inventory. |
| 72 | Is 240 s completion better than 270 s? | failed mechanics smoke | Changed action never executed in a valid window; two 1013 closes. Nothing learned about timing. |

One change at a time was not the mistake; it is good causal engineering. The
mistake was treating a handful of windows as enough to decide economics and
then immediately tuning again. Going forward, a 3-5-window run is only a smoke
test and is green only if the changed action actually fires. A strategy result
requires a frozen, predeclared cohort.

## Aggregate strategy evidence

The unchanged arms can be aggregated more honestly than the generation labels.
The totals below come directly from the immutable Gen60-72 archives, plus the
exact recovered Gen65 fill-derived row documented after its settlement crash.

### Mint control

- 35 valid strategy-windows;
- approximately 29 complete five-set cycles and six incomplete cycles;
- observed completion about 82.9%;
- realized PnL **-$10.30**;
- neutral mechanics **+$4.70**;
- about 30 unmatched shares in total.

Completed cycles earned about $0.15. The six incomplete cases lost about $2.44
on average. That payoff mix requires about **94.2% completion** merely to break
even in the paper model. The 95% Wilson interval for the observed 29/35
completion rate is approximately **67.3%-91.9%**, with its upper bound below the
observed break-even requirement. Real fill and rebate model error can only make
this gate harder. The current mint strategy is falsified under its own model,
not merely under-sampled. Retain one small arm only as a fill/adverse-selection
control.

### Current timed-completion lineage

Across the 23 matched Gen64-72 windows where `basket99t270d` and baseline both
exist:

| Metric | Baseline | Timed completion | Difference |
|---|---:|---:|---:|
| Realized PnL | +$4.37 | +$3.82 | -$0.54 |
| Neutral PnL | +$13.47 | +$22.92 | **+$9.46** |
| Aggregate adverse floor | -$26.96 | -$7.55 | **+$19.42** |
| Unmatched shares | 80.86 | 60.90 | **-19.96** |

The mechanism reduced tail exposure and increased outcome-neutral value. Its
slightly worse realized PnL is settlement luck, exactly why headline PnL is the
wrong primary metric here. But these windows are tiny, mostly feed-tail exposed,
and the taker-completion model is optimistic. This is a candidate for later
frozen validation, not evidence of a profitable bot.

## Current winner evidence

The fresh six-hour artifact contains at least four different profiles:

1. **Pair accumulators:** `0x3048...` and `0xdf4...` buy both outcomes, do not
   sell, and have aggregate buy-pair proxies below $1. The former is mostly
   taker; the latter is mostly maker.
2. **Maker merge-recyclers:** `0x338...`, `0xcd4...`, `0xc2ad...`, and
   `0x32ed...` have high maker shares and direct merges, including roughly
   4,218 and 5,211 sets for the two wallets omitted from the first narrative.
3. **Mixed maker flow:** `0x7cf...` buys and sells both outcomes with near-100%
   maker flow. Its aggregate buy pair is above $1 and sell pair above $1, but
   aggregation cannot prove ordered profitable cycles or the inventory source.
4. **Inventory plus direction:** `0x0cb...` and `0xcb92...` carry large residual
   or sell-heavy flow. Their headline PnL cannot be called neutral market making.

Important correction: `tools/wallet_metrics.py` computes the current
`buy_pair_sum` from full-lifecycle average Up and Down acquisition costs times
matched aggregate shares. It is **not FIFO cycle matching**. It can make a
directionally successful lifecycle look like a cheap pair after the losing
token collapses. Therefore the fresh aggregate is a wallet-discovery tool, not
proof that a $0.99 rule or any exact sequence wins.

The historical `0xb27...` analysis did use proper FIFO pairing and supports
cheap, rapid pair accumulation as one real mechanism. It does not support our
exact $0.99 cap: many marginal pairs exceeded $1 while earlier cheap inventory
kept the rolling basket attractive, and its later $1.015 cohort lost. The exact
cap remains a heuristic requiring winner/loser validation.

No current artifact establishes a private directional signal. The older
`0x0cb...` flow sometimes aligned with momentum but did not outperform the
public tape on disagreement cases. The next forensics must condition residual
inventory on outcomes and join exact fills to the official TWAP before making
any prediction claim.

## Latency and transport

The normal public path is now well measured:

- source/event age roughly 9-15 ms on the healthy path;
- application queue residence normally <=9 ms;
- strategy loop around 1 ms;
- simulated action activation roughly 65-85 ms;
- sparse source-age tails around 0.4-4 seconds;
- authenticated order POST/cancel latency still unmeasured.

The 1013 close remains unresolved. Measured client-frame and application queues
were shallow at the failures; planned refresh and a larger client queue were
falsified. The leading survivors are a network delivery stall, provider-side
per-connection shedding under bursts, or an OS/TCP receive-path condition.
Client strategy-loop saturation is now low probability.

The smallest discriminating experiment is concurrent, long-horizon observation
of the current receiver and a bare identical-subscription receiver, plus an
identical receiver on a second network, with last-frame gaps, subscription
events, TCP retransmission/zero-window counters, and raw frame timestamps.

## Paper fill-model risk

The two reviewers agreed on the most load-bearing risks, and code audit supports
them:

1. `paper/taker.py` consumes all displayed depth atomically at the observed
   prices. It has no competing takers, partial market-order execution race,
   impact, or authenticated-latency distribution. Timed completion relies on
   exactly this optimistic path.
2. The 65 ms action proxy is constant. In particular, a completion opportunity
   that appears after the time gate can be swept in the same decision pass, and
   the resting maker cancel is atomic with the modeled taker action. Real
   cancel/replace and POST latency tails are absent.
3. order-book freshness is token-level. Individual untouched levels have no age
   or expiry, so stale or ghost displayed depth can remain sweepable even when
   the latest token event is fresh.

Two Opus claims are rejected or narrowed after code audit:

- A through-price print ignoring same-price `queue_ahead` is not automatically
  optimistic; price priority implies that a genuinely later worse-price trade
  crossed the better level. The model also caps the fill by the public print
  size. Missing/out-of-order prints remain a data-quality risk, but this is not
  the strongest flaw.
- `invalidate()` does not delete filled inventory. It cancels simulated resting
  orders and persists cash/shares/committed capital in `invalid_windows`.
  However, aggregate economics exclude invalid windows; because disconnects
  may correlate with bursty adverse regimes, reports should add a separate
  settlement of **known pre-disconnect exposure** rather than silently treating
  the valid cohort as representative.

## Signal reconciliation

The review packet deliberately asked both models to verify an approximate but
mis-scoped count. Both caught the inconsistency.

- The comparable published running chain is **20/30 through Gen71**, then
  **22/33 through Gen72**.
- At `abs(signal) >= 1 bp`, the same chain is **12/17** through Gen72.
- At `abs(signal) >= 2 bp`, it remains **6/7**.
- Reconstructing every per-window observation printed in the reports, including
  two reconnect-invalid Gen61 observations omitted from later running totals,
  gives **24/35** overall. The immutable archives alone currently yield 18/29
  because Gen65 had recovered settlements outside a final DB, one Gen66 signal
  was described but not reconstructable by the audit, and the two pre-fix Gen61
  outcomes were not persisted in the later schema.

The denominator changes are themselves disqualifying for threshold selection.
All one- and two-basis-point buckets were selected adaptively, observations are
serially dependent, and missing opening samples correlate with feed health.
Future evaluation must compare economic improvement versus the contemporaneous
CLOB market probability, not direction accuracy versus 50%, and use an
untouched time-ordered holdout.

## Independent reviewers

### Qwen 3.8 Max

Exact `qwen3.8-max` completed 40 successful read/search calls, processed about
1.49 million total tokens including cache, and made zero file changes.

Scores: mechanism 55/100, economics 30/100, scale 5/100, reliability 25/100.
It gives the timed basket substantial credit for having the right skeleton but
calls the system a good measurement instrument and a poor strategy replica.

Its preferred sequence: transport attribution; then a frozen 24-hour baseline
versus T+270 cohort with one mint control; then deeper winner lifecycle
forensics. It also identified the missing raw-event recorder required for
deterministic replay.

Weakness in its answer: it could not query SQLite and initially reconstructed
the signal denominator from inconsistent prose. The archive/report
reconciliation above supersedes its provisional count. Its mint verdict still
survives the exact 35-window aggregation.

### Claude Opus 5 Max

Exact `claude-opus-5` ran at max effort, standard service, with fast mode off,
for 26 turns. It did not edit the repository. Its review was automatically
persisted by Claude plan mode outside the repository.

Scores: mechanism 30/100, economics 12/100, scale 3/100, reliability 22/100.
It is harsher because the fresh pair-sum proxy is not FIFO, the exact $0.99 cap
is unsupported, and the flagship taker-completion path is the most model-
flattered action.

Its preferred sequence: transport isolation; then a zero-runtime seven-day
winner/loser study testing whether pair cost actually predicts profit; then a
signal-versus-market study. It correctly caught the Gen72 signal off-by-one and
the fact that Gen70-71's combined completion improvement had been mis-scoped in
one summary.

Rejected/narrowed claims: its `actual/implied pair edge` multiples divide total
wallet volume by an aggregate matched-share price proxy; volume includes
unmatched and sell flow, so those exact multiples are not economically valid.
It also inferred 36 mint windows/-$12.25 where the direct Gen60-72 aggregation
is 35/-$10.30, and overstated the through-print and invalidation issues as noted
above.

## Adjudicated closeness

| Dimension | Qwen | Opus | Adjudicated |
|---|---:|---:|---:|
| Mechanism | 55 | 30 | **40/100** |
| Economics | 30 | 12 | **20/100** |
| Scale | 5 | 3 | **3/100** |
| Reliability | 25 | 22 | **20/100** |

The mechanism score recognizes paired maker bids and rolling completion. It is
not higher because we lack winner-proven replenishment, merges, rebate truth,
direction decomposition, and realistic completion execution. The economics
score recognizes positive modeled neutral deltas but heavily discounts tiny,
lagged cohorts and optimistic taker fills. Scale and reliability are not close.

## Three proposed experiments — not started

### 1. Transport attribution and raw-event capture

- Hypothesis: 1013 is caused by the network/provider receive path, not strategy
  processing.
- Frozen variants: current receiver, bare identical-subscription receiver on
  Ireland, and bare receiver on a second network; concurrent.
- Primary metric: 1013 per connection-hour and receive-gap/TCP state before
  each close.
- Sample/stop: 24 hours or 10 closes.
- Decision: same-host arms fail together → path/provider; runner alone fails →
  client integration; both locations fail → provider behavior. Capture raw
  timestamped frames to create the replay substrate at the same time.

### 2. Seven-day winner-and-loser lifecycle forensics

- Hypothesis: properly FIFO-matched cheap paired acquisition and/or merge-
  recycling predicts neutral profit after fees/rebates better than directional
  residue.
- Cohort: selected on activity/volume before PnL, including winners and losers;
  time-ordered discovery and holdout periods.
- Primary metrics: FIFO marginal and rolling pair cost, completion delay,
  neutral/outcome decomposition, residual outcome rate, merge provenance,
  proxy/funder splits, and actual rebate income.
- Decision: if cheap-pair/merge features fail to predict holdout neutral PnL,
  retire the $0.99 board premise. If residual direction dominates, stop calling
  the wallets market makers and test direction separately.

Experiments 1 and 2 are independent and can run in parallel without touching
money or changing strategy behavior.

### 3. Frozen forward completion cohort, only if 1 and 2 pass

- Prerequisites: attributed/stable feed, raw recorder, per-level age handling,
  non-atomic latency/partial taker sensitivity, and a 3-5-window trigger smoke.
- Frozen arms: `basket99`, one evidence-selected completion arm, and
  `mintcycle5` control; no more than three active hypotheses.
- Primary metrics: paired per-window neutral PnL delta and adverse-floor delta;
  secondary completion rate, unmatched shares, pair-cost distribution, and
  validity-adjusted known-exposure outcomes.
- Sample: at least 200 valid BTC windows with no tuning; hourly block bootstrap
  and a predeclared untouched time holdout.
- Pass: lower confidence bound of net neutral improvement above zero and better
  validity-adjusted adverse floor. No promotion from an interim point estimate.

The TWAP observer may continue passively inside this cohort, but no quote gate
or strength threshold is an active arm.

## Stop doing

- no more 240/270 timer tuning from non-triggering cohorts;
- no p95/p100 mint-floor tuning;
- no larger inventory cap or clip experiments without a binding-state reason;
- no dust-tolerance variants that buy more unmatched inventory;
- no queue-size/reconnect tuning without transport attribution;
- no winner claims from aggregate `both%` or non-FIFO pair proxies;
- no signal threshold selected from the current 35 observations;
- no new strategy generation number for instrumentation-only work.

