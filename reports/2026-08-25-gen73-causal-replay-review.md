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
historical falsification. It has now been removed from the always-on board and
retained only in tests and frozen artifacts. No strategy is approved for real
orders.

The real Gen73 progress is a better instrument: a loss-intolerant causal
capture, one shared live/replay engine, exact replay provenance, clock-skew
failure handling, and passive official/Binance/Deribit capture on Ireland.

## What the iteration tests

Gen73 initially used only two paper controls:

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

The post-calibration review removed `mintcycle5` from the always-on board. Its
historical falsification remains in fixtures and frozen artifacts; repeatedly
running an instant-inventory, optimistic-queue simulator was not an independent
experiment.

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
- The calibration board was reduced from five arms to `basket99` and
  `mintcycle5`; the post-calibration board was reduced again to `basket99` alone.
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
- Adverse-selection simulation research shows that modelling prices separately
  from fills inflates a market-making backtest. Fill probability and post-fill
  adverse drift must be estimated jointly from the same event tape.
- Limit-order survival research treats cancellation/cutoff as censoring and
  conditions time-to-fill on high-frequency book state. The first implementation
  here should be an interpretable Kaplan-Meier/competing-risk baseline with
  proper scoring, not a transformer fitted to a few windows.
- A 2026 Kalshi study reports that one-sided order flow predicts maker losses.
  This supports testing flow toxicity as an initiation gate, but it is a research
  direction rather than evidence that the same rule transfers to BTC five-minute
  Polymarket books.

The first research target is conditional second-leg completion after a first
fill, because unmatched inventory—not a missing directional forecast—is the
observed loss mechanism. Only after that hazard and loss distribution are known
may an external-feed toxicity signal gate the *opening* of a new pair. It must
never suppress completion of an already open leg. Signal strength must be
derived from conditional adverse markout, pair surplus, and non-completion loss,
not an arbitrary basis-point cutoff. A reactive cancel after a move may be too
late under the current 50 ms changelog value, and authenticated cancel timing
remains unmeasured.

Official references:

- https://docs.polymarket.com/changelog/predictions
- https://docs.polymarket.com/concepts/order-lifecycle
- https://docs.polymarket.com/market-data/chainlink-twap
- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/market-makers/maker-rebates
- https://arxiv.org/abs/2607.26245
- https://arxiv.org/abs/1312.0563
- https://arxiv.org/abs/2409.12721
- https://arxiv.org/abs/2306.05479
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6615739

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

A 22:58–23:27 UTC prospective engineering run then exercised the reconnect
path before being finalized for post-review remediation:

- 668,422 raw frames replayed into 41 cohort records; exact finalized-cohort
  parity held for 29 fills, eight settlements, and four invalid rows.
- One CLOB provider `1013 slow consumer` close invalidated the active window.
  Local queue residence was at most 9 ms, while upstream event-age tails reached
  4.682 seconds. This is not a one-second local loop.
- `basket99`: four valid, all lagged windows; -$7.39 realized, -$1.19 at the
  optimistic 50-cent residual mark, -$7.39 valid-window adverse floor, 12.4
  unmatched shares, and -4.34/-4.60/-6.37 cents per-share markout at 1/5/15 s.
  Its filled reconnect-invalid window adds a -$1.90 known-inventory floor, for
  -$9.29 across observed valid plus invalid exposure.
- `mintcycle5`: +$0.60 across four valid subsidized paper cycles, but its
  reconnect-invalid inventory adds -$2.10, producing a -$1.50 observed floor.
  This and the negative 35-window history reject continuous mint monitoring.
- Dataset SHA-256:
  `41bad4847e3abb31711dd6579c9095e6c46a0f665d7f8362e51d816f2ec5181e`.
- Replay SHA-256:
  `d6ceb877fe7d9d191cbfe344e9d27da1e7c49d31229c81b333fc8688a184bc97`.
- SQLite SHA-256:
  `ddd0d49a985a4c198e7279af86483bc572139488b61f55d453089ad3010c8ec3`.

The passive `gen73-crossvenue-prospective-20260825T2300Z` RTDS/Binance
spot/Binance futures/Deribit capture remained active during remediation. The
paper runner was stopped cleanly so its manifests and database could be frozen.
Real-order, executor, and mintbot place paths remained stopped throughout.

## Post-calibration independent review and adjudication

Both reviewers audited immutable commit
`161868a5bc2a59cc1167848989360d109562686c`. Claude Opus 5 ran at max effort;
Qwen ran as exact `qwen3.8-max`. The local review packet intentionally omitted
the large gzip chunks, so they could verify the metadata chain but not recompute
the raw replay themselves. The complete Ireland artifacts were separately
replayed before and after the reconnect run.

| Dimension | Opus 5 Max | Qwen 3.8 Max |
|---|---:|---:|
| Mechanism identification | 38 | 45 |
| Replay integrity | 72 | 74 |
| Executable economics | 9 | 12 |
| Scale readiness | 5 | 6 |
| Operational reliability | 40 | 40 |

Accepted defects and corrections:

- A ledger-writer close error could skip capture finalization. Ledger and
  capture closure are now independent and all closure errors are surfaced.
- Disconnect replay used the marker wall time rather than the exact live engine
  invalidation time. Disconnect markers now carry that observed time.
- A raw writer thread error could remain latent until shutdown. Further submits
  now fail immediately, terminating evidence collection.
- The comparator could vacuously pass empty records and did not bind the replay,
  dataset, and database run identities. It now rejects empty runs, embeds the
  dataset hash/label, and checks ledger label/board/model metadata.
- Telegram and the full report ranked on neutral PnL, which marks every residual
  token at $0.50 and can reward near-worthless inventory. Ranking now uses the
  adverse inventory floor, including invalid-window known exposure. Neutral PnL
  and FIFO pair edge remain diagnostics only.
- The execution-model identity was an all-`paper/*.py` glob. It now has an
  explicit 25-file execution/timing boundary; reporting and replay-tool edits no
  longer masquerade as economic-model changes.

Reviewer disagreements were resolved as follows:

- Opus recommended removing mint; Qwen recommended retaining one negative
  sentinel. Continuous mint was removed, while its many focused unit fixtures
  and frozen 35-window negative artifact retain the regression value without a
  misleading always-on PnL stream.
- Opus prioritized completion hazard; Qwen supported a pre-quote toxicity gate.
  Completion comes first. A later toxicity signal may gate only initiation of a
  new pair and must be tested on a genuinely future holdout.
- Comparator equality remains explicitly scoped to finalized cohort records.
  Shadow `reference_prices`, `resolved_windows`, and open-at-stop engine state
  are separate evidence and are never implied by the word parity.

Post-remediation mechanical gate: **126 tests passed**. Ruff and mypy are run on
the changed execution/research files, not misrepresented as a clean legacy-repo
gate; old root-level exploratory scripts retain known lint debt.

Corrected one-probe board hash:
`5a887398744ef74a8c2f72278ee07b3664ce9c62262be7bcf9577a211ef10f66`.
Execution-model-v2 hash:
`c806b8389691239fa38332cfe6b84e8c4413a97a66eec33f58d8e7ddec096e29`.

The corrected Ireland paper runner started at 23:37 UTC with capture label
`gen74-fillprobe-20260825T2340Z`, the hashes above, BTC only, 65 ms action
proxy, 400 ms stale cutoff, 10 ms decision cadence, and a 15-minute Telegram
summary. Its first heartbeat had exact accepted/written capture counts, zero
drops/cap/error/future timestamps, and no reconnect. Local queue residence was
at most 6 ms while the same interval already contained a 4.270-second upstream
event-age tail. SQLite `integrity_check` returned `ok`, and its run metadata
matched the capture label, board hash, and model hash. The passive four-feed
cross-venue capture remained uninterrupted. No real-order process was started.

At 00:06 UTC the corrected run had three full scored windows plus one fill-free
startup invalidation. Nine settled maker fills produced -$4.36 realized and
adverse-floor PnL despite +$1.48 paired edge and +$2.23 at the optimistic
50-cent residue mark; 13.2 shares remained unmatched. Share-weighted completion
delay was 79.8/114.6 seconds p50/p90, and maker markouts were negative by
1.92/1.32/0.72 cents per share at 1/5/15 seconds. All three windows were
feed-tail exposed. The latest heartbeat was back to 19/134 ms upstream p50/p90
with a 4.319-second lifetime maximum; local queue residence was at most 16 ms
and the loop p50/p90 remained 1 ms. Capture drops, caps, errors, future
timestamps, and reconnects remained zero. Three windows are engineering
evidence only, but the loss mechanism is already the same unmatched-residue
mechanism seen in the earlier tapes.

## Terminal pair-completion economics

`tools/pair_completion_economics.py` reconstructs FIFO first-leg lots from
immutable paper ledgers and reconciles paired surplus plus directional residue
to settlement PnL. Acquisition cost comes from signed cash, so `taker_buy` fees
are included rather than silently replaced with displayed price. It refuses
conflicting finalized copies, reports unfinished source rows explicitly, hashes
every input, and refuses to overwrite output. This is terminal accounting, not
yet a censored survival/hazard model.

On the corrected Gen73 prospective ledger, the exact terminal cohort was four
settled plus two invalid windows; one unfinished slug was excluded:

- 26.70 first-leg shares opened, 9.30 completed, and 17.40 remained unmatched:
  **34.83% completion**.
- Share-weighted completed-pair delay was 58.38 seconds p50 / 80.28 seconds p90.
- Completed pairs earned only $0.1217 total, or $0.01309 per completed share.
  Residue cost $9.4080, or $0.54069 per incomplete share.
- The observed adverse floor was **-$9.2863**. Under those empirical unit
  economics, the algebraic zero-adverse-floor completion fraction was
  **97.64%**.
- FIFO mechanism PnL reconciled to every settled row within
  `3.9e-16` dollars.

Artifact SHA-256:
`bbece70dade91e8b09e44f6ea97eddae82248ce0f28c35996e6971e6328b84af`.

The dirty Gen60--72 mechanics archive points the same way but is not a
validation cohort. Across 51 finalized rows (33 settled, 18 invalid), 363.18
shares opened, 234.21 completed, and 128.97 remained unmatched: 64.49%
completion versus an 86.08% algebraic zero-floor rate, with a -$45.1956 adverse
floor. Only three settled windows were clean; 30 were lagged. Five of seven
reconnect-invalid windows carried exposure and contributed a -$8.31 floor.
Eighteen unfinished source rows were excluded, including three exact
fill-derived recoveries accepted by old reports; adding those three changes the
completion diagnostic to 63.39% and the zero-floor rate to 85.39%, without
changing the outcome-neutral risk conclusion. Historical artifact SHA-256:
`c8ffb75a06da352a8025aed879f72c7bb4fd6d3a96ea6dfc3b5f7616a2fb1394`.

These percentages are not independent share trials, confidence bounds, or
expected-PnL break-evens. Directional outcome luck or a real signal can still
make realized residue profitable; this screen measures whether paired surplus
alone insures the residue, and it plainly does not. A real conditional hazard
needs stable lot IDs, exact right-censor/competing-event times, contemporaneous
queue/book state, all
unfinished exposure followed to official settlement, contiguous time blocks,
and inference clustered by market window. The present result fails an
outcome-neutral robustness gate, so `basket99` cannot be promoted. Keeping it
on Ireland is justified only as a fill/queue-mechanics probe.

## Immediate-completion counterfactual

The first replayed improvement changed exactly one economic field: clone
`basket99` and set `buy_taker_after_s=0`. After a first maker fill, the existing
engine waits through the captured 65 ms action proxy and then takes the full
opposite depth only when taker fees included still preserve the rolling $0.99
basket cap. The A/B board hash is
`0d576e12a2e5d769fcdad44dbd71cf8c57d093be42cfaeeb0c556383daa8633f`.

Both complete Ireland tapes were restored locally and every chunk matched its
manifest. The prospective tape replayed 668,422 raw frames, 668,430 decoded
events, and 163,591 actual ticks with zero parse errors. Across its four settled
and two invalid windows, the candidate found **zero** taker-completion shares;
every finalized PnL, neutral, floor, and residue endpoint equaled baseline. It
completed three five-share cycles only in the seventh open-at-stop window,
turning a -$1.30 baseline snapshot floor into +$0.15211 after $0.19789 fees.
That is a censored complete-set snapshot, not settled or prospective evidence.

The earlier calibration tape replayed 522,739 frames, 522,745 events, and
131,633 ticks with zero parse errors. Here the candidate took 30 shares across
three settled windows. It increased completed-pair shares by 25, but worsened
the aggregate adverse floor by **-$1.95810**, neutral value by -$1.78810, and
realized PnL by -$1.61810; terminal unmatched inventory rose by 0.34 shares and
finalized taker fees were $0.38270. Immediate completion freed capacity, then
the unchanged initiation policy opened new first legs and recreated the residue
risk. The cap protects completed pairs, not the next exposure cycle.

The candidate is therefore rejected and was not added to the active board.
Tuning a cooldown or cutoff on these same tapes would be post-hoc overfitting.
Both replays explicitly allowed captured-v1/current-v2 model drift, so they are
historical current-engine screens rather than parity or deployment evidence.
Authenticated POST/cancel latency, the documented 50-vs-250 ms taker-hold
conflict, real depth, partial fills, and cancel/take races remain unmeasured.

Counterfactual artifact hashes:

- prospective ledger/report:
  `aa8c811a911155eeb94cc4e0f87e3d3ac93fa941d893ff0624a3e22fd2285a41` /
  `339df739c56aee044d924e494564fa6be4aa895b3d5d9439cd39c3c42396eab5`;
- calibration ledger/report:
  `afa7fb3050c9fcd0e9fb580662a4812af89742393e1c0901d28ca442b93036b9` /
  `384ca616a6dbae5aba17f3a1f3b2d3adf9fa0a901be2c701491d156dc29aba6e`.

Current mechanical gate: **132 tests passed**. Ruff and mypy are green on the
four new tool/test files, and `git diff --check` has no content error (only the
expected Windows line-ending warning).

## Decision process from here

1. Keep passive causal covariate capture running and run only the corrected
   `basket99` fill probe, ranked on the adverse floor.
2. Estimate the second-leg completion hazard and non-completion loss from the
   immutable capture, conditional on the first fill and contemporaneous book.
3. Freeze a prior block and measure a maker fill-degradation surface: queue
   survival/cancellation bounds, action-latency sensitivity, and displayed-depth
   haircuts. This produces the profitability buffer the simulator must clear.
4. Collect enough immutable pre-period windows to estimate the endpoint variance
   and block dependence. Predeclare one horizon/futility rule and one future
   replication cohort.
5. Continue winner lifecycle and passive cross-venue event studies. A toxicity
   arm may gate new-pair initiation only after discovery, then must clear fees,
   timing, multiple-testing, and an untouched holdout.

This process can reject ideas quickly. It cannot manufacture a winner quickly.
Adding dozens of always-on strategies before these gates would be cheap in CPU
and expensive in false confidence.
