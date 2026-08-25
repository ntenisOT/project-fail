# Gen73 causal replay and strategy review — 2026-08-25

## Verdict

The proposed research loop is correct, but our prior interpretation was not.
Continuous paper capture, faster replay, winner forensics, and external-feed
event studies should run in parallel. Strategy code should enter the board only
after a causal, fee-aware discovery result and should face a frozen future test.

There is still **no executable edge**. The strongest historical result was
misframed: cheap paired acquisition is a persistent wallet behavior, but it does
not predict actual wallet profit. The corrected exact-cohort actual-PnL AUC is
0.471, or 0.482 without the holdout-activity filter. Mint remains a falsified
control. No strategy is approved for real orders.

The real Gen73 progress is a better instrument: a loss-intolerant causal
capture, one shared live/replay engine, exact replay provenance, clock-skew
failure handling, and passive official/Binance/Deribit capture on Ireland.

## What the iteration tests

Gen73 intentionally has only two paper controls:

| Control | Question | What would count |
|---|---|---|
| `basket99` | Can our queue model reproduce cheap paired inventory without dangerous residue? | Exact replay parity first; later, profitability only after a predeclared fill-degradation buffer and future cohort |
| `mintcycle5` | Does even one repeated mint-style cycle survive the residue problem? | Falsification/control only; it cannot be promoted from current evidence |

The passive cross-venue recorder is not a third strategy. It collects causal
RTDS TWAP60, Binance spot/futures, and Deribit observations for later event
studies. A rule may be proposed only after discovery and must then be frozen for
a non-overlapping holdout.

Three to five windows are an engineering calibration for capture loss, event
ordering, lifecycle completeness, and live/replay equality. They are not an
economic sample.

## Corrected winner result

The exact frozen 40-wallet BTC cohort covers 2,016 five-minute windows from
2026-08-18 20:40 through 2026-08-25 20:35 UTC. Wallets were fixed from the
original discovery-period activity selection and were not re-ranked during the
restatement.

| Result | Value | Meaning |
|---|---:|---|
| Discovery cheap-pair score → holdout neutral-PnL AUC | 0.9067, n=34 | Pair cost persists and neutral accounting rewards sub-$1 pairs |
| Discovery cheap-pair score → holdout actual-PnL AUC | 0.4706, n=34 | No actual-profit discrimination |
| Actual-PnL AUC including every wallet with discovery FIFO evidence | 0.4824, n=37 | Removing the holdout-activity gate does not rescue it |
| Discovery → holdout FIFO pair-sum Spearman | 0.8258, n=34 | Strong execution-style persistence |
| Inactive/no-FIFO holdout attrition | 3 / 3 | Future-activity selection was non-trivial |

Artifact: `out/gen73-lifecycle-btc-7d-v2-frozen.json`, SHA-256
`ecd98ac5d03427e26f9324b0327c6b2f0f73539d4e62f50773cd1f8552ae0825`.

This is the key distinction: we found a stable *way of trading*, not a stable
way of making money. Pair99 remains useful for calibrating maker fills, but the
wallet study no longer supplies an economic promotion argument.

The historical Binance association screen is also rejected as a strategy.
Maker/taker direction was near chance, trade-time intervals were censored, the
holdout covered only one UTC day, and the 36 correlated specifications were
exploratory. Artifact:
`out/gen73-winner-signal-through-aug24-v2.json`, SHA-256
`5cb6c6f43d77d1319c37b95729627c42bd3ea55b624f7d571f0c8fa7f9447409`.

Exact historical Chainlink TWAP60 values cannot be reconstructed from Binance
or Deribit. Polymarket documents that RTDS has no snapshot, history, or replay
after a disconnect, and Chainlink does not publish enough of its custom
sampling/weighting behavior to reproduce the value independently. Prospective
capture is therefore required.

## Prior paper evidence and sample-size audit

- Gen60–72 contain only 33 persisted valid unique `basket99` windows, all in one
  6h20m calendar span. Thirty are lagged and only three are clean.
- Eighteen baseline windows are explicitly invalid, and another 18 have fills
  without an immutable settlement/invalid row.
- Strategy aliases heavily duplicate the baseline; configuration semantics also
  changed across generations.
- The stable T+270 comparison has only 15 persisted paired windows and the
  treatment changes behavior in only three.

Therefore the per-window endpoint variance, serial dependence, prospective
invalid rate, trigger probability, and smallest useful effect are not identified.
The required economic sample size is honestly **unknown**. The old 200/288-window
numbers were invented convenience thresholds and have been removed from current
documentation.

Mint's aggregate remains negative: 35 valid windows, about -$10.30, roughly 29
complete and six incomplete cycles, 82.9% completion against approximately
94.2% break-even under that paper mechanic. New mint variants are prohibited
unless independent forensics first establish a mint-specific edge.

## Independent hostile review

Both reviewers received the frozen `out/gen73-review-packet.md`, relevant code,
artifacts, test claims, and explicit instructions to challenge the thesis. Their
reviews were read-only and predate the remediation below; they are not falsely
presented as reviews of the final dirty-tree revision.

| Dimension | Claude Opus 5 Max, max effort | Qwen 3.8 Max | Adjudication |
|---|---:|---:|---|
| Mechanism identification | 42 | 52 | We identify pair behavior, not profit or universal winner strategy |
| Replay integrity | 58 | 72 | Strong structure, but deployment parity still needs the live calibration |
| Executable economics | 12 | 18 | No fill calibration or prospective economic pass |
| Scale readiness | 8 | 6 | Nowhere near scale |
| Operational reliability | 35 | 30 | Fail-closed work improved; provider disconnects and venue timing remain risks |

Accepted findings:

- The local Windows clock was about 1.64 seconds behind Binance. Negative event
  ages were being clamped and could disable freshness protection.
- Byte caps could silently turn a long capture into wasted time.
- Valid-window economics are conditioned on surviving disconnects.
- The 0.9067 AUC is largely pair-accounting plus style persistence, not profit.
- Maker queue fills and all taker mechanics remain uncalibrated.
- A replay-vs-live differential invariant was missing.

One reviewer statement was narrowed: assigning zero displayed queue ahead is
correct when an improved price level is genuinely empty at activation. The
remaining race, hidden-joiner, cancellation, and adverse-selection uncertainty
still requires stress bounds and later calibration.

## Remediation completed

- `book`, `price_change`, and trade events more than 50 ms in the future now
  invalidate the affected paper window; negative age is counted, never clamped.
- Feed health reports future timestamp counts and maximum future skew.
- Raw or causal capture cap/queue loss raises immediately and stops the run.
- The raw frame, processed event, actual decision tick, and lifecycle manifests
  are hash-bound. Replay consumes only the successfully processed prefix.
- Live paper and replay now use the same `CohortEngine`; the differential fixture
  asserts exact record equality.
- Replay reports open-at-end and finished-but-unresolved lifecycle residue and
  validates duplicate/out-of-order markers.
- Invalid-window reporting includes the known inventory floor rather than
  silently dropping filled exposure.
- The board was reduced from five arms to `basket99` and `mintcycle5`.
- `GO_LIVE.md` and the README no longer claim a four/five-arm board or an
  arbitrary 200/288-window promotion gate.
- The lifecycle tool can freeze and reuse the exact original wallet cohort,
  preventing accidental re-ranking during a restatement.

Mechanical gate: **122 tests passed**, Ruff passed, mypy passed on 22 changed
source files, and `git diff --check` passed. The single Python 3.14 deprecation
warning comes from import machinery, not project behavior.

## Current official facts

Polymarket's current changelog says the crypto taker delay changed from 250 ms
to 50 ms on August 17, while the current order-lifecycle page still says 250 ms.
Until an authorized authenticated measurement resolves the contradiction, a
taker result is hold-sensitive and cannot promote a strategy. The 65 ms paper
action delay remains a sensitivity proxy, not measured POST/cancel latency.

The changelog also says current five-minute crypto markets use a 60-second
Chainlink TWAP for both opening and settlement values. RTDS topic
`crypto_prices_twap_sixty` is therefore the correct prospective official
reference. Binance and Deribit are covariates, never substitutes for the label.

Current crypto fees are `shares * 0.07 * p * (1-p)` for takers, zero for makers,
with a 20% fee-funded maker-rebate pool allocated by fee-equivalent share per
market. Paper keeps rebates separate because the actual payout depends on the
rest of each market's maker flow.

Two external research results constrain the next experiment:

- OpenMarket's synchronized Polymarket/Binance study reports that a walk-forward
  model using 43 microstructure features did not beat Polymarket's own order
  book out of sample and lost after its stated costs. Its synchronization-free
  event study measured a median roughly 347 ms Polymarket quote response after
  large Binance moves. That argues for toxicity/risk gating, not another naive
  directional taker model.
- Queue-reactive limit-order-book research models order arrivals and
  cancellations conditional on the current queue state. Our capture can support
  that calibration; a static displayed-depth assumption cannot.

The practical candidate is therefore a pre-quote eligibility/inventory throttle
whose signal strength is derived from conditional adverse markout versus the
available pair surplus. It is not yet a strategy arm. A reactive cancel after a
move may be too late under the current 50 ms changelog value, and authenticated
cancel timing remains unmeasured.

Official references:

- https://docs.polymarket.com/changelog/predictions
- https://docs.polymarket.com/concepts/order-lifecycle
- https://docs.polymarket.com/market-data/chainlink-twap
- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/market-makers/maker-rebates
- https://arxiv.org/abs/2607.26245
- https://arxiv.org/abs/1312.0563

## Ireland calibration and current deployment

The 22:33–22:56 UTC engineering calibration ran paper-only with BTC, a 65 ms
action proxy, 400 ms stale cutoff, 10 ms decision cadence, and capture label
`gen73-cal-20260825T2235Z`.

- Raw: 522,739/522,739 frames written, zero drops/cap/error.
- Causal: 654,378/654,378 records written, zero drops/cap/error.
- Strict replay: zero parse errors; board/model hashes matched; six opened, five
  finished, four resolved, one open-at-stop, and one finished-unresolved tail.
- Exact ledger parity: all 30 cohort records matched—22 fills, six settlements,
  and two startup-invalid rows.
- Three complete scored windows were all feed-tail exposed. `basket99` ended
  -$2.07 realized, +$2.55 neutral, -$2.82 adverse floor, and 10.8 unmatched
  shares. `mintcycle5` completed three one-pair cycles for +$0.45; this does not
  override its negative 35-window history.
- Ordinary CLOB age was usually tens of milliseconds, but exposed tails reached
  4.628 seconds while local queue residence stayed at or below 8 ms.
- Deribit sent clean `1000 OK` closes at exact 600-second intervals and the
  recorder resubscribed twice; the other three sources stayed connected. No raw
  source lost or capped data.

Calibration artifact hashes:

- Paper dataset:
  `64add7c9fbc91dcb42fb6e83060fc619e94d0a018f49da6c4b28942e48e1e614`.
- Replay JSON:
  `2739727171e466f3abd6e4521122f0a8c799b448f4e207e9f1bd7c533a53f96c`.
- Cross-venue dataset:
  `4b559f09bb6a231f99610a204548105577fb8da206cba503c9a53e902d467821`.

A fresh prospective paper/capture run started at 22:58 UTC and is now the only
active runner:

- `paper`: label `gen73-prospective-20260825T2300Z`, same frozen BTC/timing board,
  Telegram report every 15 minutes.
- `crossvenue`: label `gen73-crossvenue-prospective-20260825T2300Z`, passive
  RTDS/Binance spot/Binance futures/Deribit, bounded to 24 hours.
- Board hash:
  `85f335bf649bea7d7960507b164bfc5bb08b233b0067261efa46a84e603f2a77`.
- Execution-model hash:
  `a1a3a75397fa276ea4b084476f4079bd3b2229e40fe243db6ce9107605c98f78`.
- NTP synchronized, 234 GB free, `.env` unchanged at SHA-256
  `3137d00c3d37e63168d796aad29d8e4e1878897a283299865ecc9c1dc36ccb30`.
- First prospective heartbeat: no drops, reconnects, future timestamps, or
  capture errors; CLOB p50 13 ms, p90 112 ms, queue residence max 3 ms. This is
  plumbing evidence only.
- The prior paper database and log were moved into `out/deploy-archive`; no data
  was irreversibly deleted.

Real-order, executor, and mintbot place paths remain stopped. The old local
shadow mintbot and paper runner were also stopped after Ireland verification to
avoid duplicate subscriptions and Telegram reports.

## Decision process from here

1. Keep the prospective two-control board and causal covariate capture running;
   monitor integrity and exposure every 15 minutes.
2. Freeze a prior block and measure a maker fill-degradation surface: queue
   survival/cancellation bounds, action-latency sensitivity, and displayed-depth
   haircuts. This produces the profitability buffer the simulator must clear.
3. Collect enough immutable pre-period windows to estimate the endpoint variance
   and block dependence. Predeclare one horizon/futility rule and one future
   replication cohort.
4. Continue winner lifecycle and passive cross-venue event studies. Implement a
   new arm only when a causal candidate clears fees, timing, multiple-testing,
   and untouched-holdout requirements.

This process can reject ideas quickly. It cannot manufacture a winner quickly.
Adding dozens of always-on strategies before these gates would be cheap in CPU
and expensive in false confidence.
