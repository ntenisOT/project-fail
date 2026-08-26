# Winner cross-venue event study: identification audit and minimum design

Date: 2026-08-26

## Decision

Do **not** implement or run a historical estimator that claims the selected
wallets reacted to Binance, Deribit, or the official reference before
Polymarket repriced. The local artifacts do not identify that sequence.

The strongest statement the current data can support is narrower: a wallet's
first *observed on-chain fill block* may be descriptively associated with an
external return measured several complete seconds earlier. That is not order
timing, not a subsecond reaction, and not causation. For maker fills in
particular, the order may have rested before the external move; the fill is an
execution/adverse-selection observation, not evidence that the maker reacted.

`tools/winner_signal_study.py` already performs the honest version of that
coarse Binance screen. Its result has been rejected as strategy evidence: the
directional association is near chance, the only holdout is one previously
observable UTC day, and the 36 specifications are correlated. Adding two more
feeds to the same timestamp mismatch would create apparent sophistication, not
new identification.

No strategy, paper, live, remote-host, or money-path code was changed for this
audit.

## What is locally available

| Artifact | What it supplies | Identification limit |
|---|---|---|
| `out/gen73-lifecycle-btc-7d-v2-frozen.json` | Frozen 40-wallet cohort and discovery/holdout boundaries; SHA-256 `ecd98ac5d03427e26f9324b0327c6b2f0f73539d4e62f50773cd1f8552ae0825` | Its analyzed holdout was already observable; it is not a new confirmatory sample |
| `out/gen73-winner-signal-through-aug24-v2.json` | V2-normalized wallet buy fills plus verified Binance spot/futures one-second archives; SHA-256 `5cb6c6f43d77d1319c37b95729627c42bd3ea55b624f7d571f0c8fa7f9447409` | Polygon timestamps are whole-second settlement-block times after off-chain matching; no order-placement time or Polymarket reprice stream |
| `tools/crossvenue_capture.py` and `tools/crossvenue_sources.py` | Hash-bound raw receive wall/monotonic timestamps for Binance spot, Binance futures, Deribit, and RTDS `crypto_prices_twap_sixty`; source/publisher timestamps where published | It does not capture the Polymarket CLOB book, `price_change`, or trade stream, so it cannot locate a Polymarket reprice |
| `out/gen73-integrity-smoke.jsonl.dataset.json` | Complete local four-source raw capture; SHA-256 `f0811a4c8e64fa3aaee5524bd7010b295458e7bfee1f49d50dc902681a31e6ee` | Only 8.5 seconds, beginning after the frozen winner period, and still no Polymarket CLOB |
| `out/gen73-calibration-review/crossvenue.dataset.json` and `paper.dataset.json` | Manifests show a simultaneous roughly 23.6-minute cross-venue/paper calibration | The referenced telemetry, event file, and raw chunks are not present locally; manifests alone are not analyzable data |
| `out/depth-20260823.jsonl` | 15,907 BTC Polymarket depth polls over about six hours; SHA-256 `0682092a906118ea91cb7ffd3bada5b87e60ce7a83fd4ecdb4db56582620feab` | Local wall-clock polling only, median cadence about 1.34 s, maximum gap about 48.64 s, no source/monotonic timestamp, no immutable dataset manifest |
| `out/refprices-20260823.jsonl` | Binance and old RTDS observations over the same interval; SHA-256 `874a3cf2c2c8e98cb7ae2aa9ec6caeacb6731cd2ea632e0e0ef188f4b07068b0` | `recorder_v2.py` subscribed to `crypto_prices_chainlink`, not the official five-minute-market `crypto_prices_twap_sixty`; it also has no futures or Deribit |
| `out/ticks-20260822.jsonl` | Older combined BTC spot/Polymarket polling; SHA-256 `06e2363492b0803ccd1db8d13d4bdbca87dda9e7c5e326bbc66fa2aa8d1f7c1a` | Roughly 2.59 s median cadence, gaps up to about 173.88 s, and only a short overlap with the six-hour files |
| `out/official-prices-btc-7d.jsonl` | Gamma `priceToBeat` and `finalPrice` window labels | Opening/final labels are not an intrawindow official-price shock series |

The six-hour August 23 overlap is large enough to tempt an analysis: 72 BTC
windows, 34 selected wallets, 94,111 in-window selected-wallet buy fills, and
1,850 wallet-window groups were observed in the read-only probe. The earliest
fill block was maker for 1,093 groups and taker for 757. It is still unsuitable
for the requested claim: the external reference is the wrong RTDS topic,
Deribit and futures are absent, the Polymarket book is polled on an unaudited
wall clock, and the cohort/sample are not an untouched holdout. It may be used
only for parser development or explicitly exploratory calibration. It must not
select a trading threshold.

## The question that is and is not identifiable

There is also a real settlement-feed regime break. Exact Gamma responses for
`btc-updown-5m-1786665300` and `btc-updown-5m-1786665600` identify a change from
the BTC/USD 30-second TWAP at 2026-08-13 23:55 UTC to the 60-second TWAP at
2026-08-14 00:00 UTC. The current Gen74/75 captures are correctly in the
60-second regime. Every historical window must nevertheless bind its own Gamma
`resolutionSource`; a date-wide hardcoded TWAP lookback would contaminate an
event study across that boundary.

The pre-change settlement-manipulation result in
https://arxiv.org/abs/2606.31675 is therefore a discovery hypothesis, not a
transferable strategy result. A post-change study may predeclare final-60/30/10
second signed spot flow, near-the-money official-reference distance, and
post-close reversal, but it must use the event's actual TWAP regime and an
untouched future cohort.

The following prospective observational question is identifiable:

> After an external or official-reference move that is safely earlier than a
> wallet's first observed fill block, is the fill side aligned with that move,
> and does Polymarket subsequently move in the same direction more than it does
> for matched control times?

These questions are not identifiable from public winner fills:

- when another wallet created, amended, or submitted a maker order;
- a subsecond winner reaction time;
- whether a maker fill was an intentional prediction rather than passive
  adverse selection;
- causal effect of the external venue on the wallet or on Polymarket; or
- a profitable signal, without execution costs and untouched out-of-sample
  economics.

The report must therefore call the event `first_observed_fill_block`, never
`order_time` or `reaction_time`.

## Minimum valid prospective dataset

One top-level immutable join manifest must bind data captured over the same
interval and clock domain:

1. The complete `project-fail-crossvenue-dataset-v1` raw capture for Binance
   spot, Binance futures, Deribit, and RTDS
   `crypto_prices_twap_sixty`.
2. The simultaneous Polymarket causal/processed feed containing every accepted
   `book`, `price_change`, and public trade event with exact handler wall and
   monotonic timestamps. The join manifest must hash the paper dataset and its
   raw/causal manifests; merely sharing a label is insufficient.
3. A frozen V2-normalized wallet-fill extract for the same resolved markets,
   containing at least wallet, slug, token/outcome, block number, log index,
   whole-second block timestamp, maker/taker role, size, price, fee, and
   transaction hash. It must be hashed and query parameters recorded.
4. Gamma `priceToBeat` and `finalPrice` for each resolved window, also hashed.
5. Capture start/end, connection intervals, dropped/capped status, raw-vs-
   processed counts, source clock-age distributions, and every excluded gap or
   reconnect interval.

The cheapest implementation is a small **offline join manifest**, not another
live feed: bind the already simultaneous cross-venue and paper causal datasets,
then run the study only after both complete local trees pass their existing
hash/integrity readers. No strategy process needs to consume these features.

## Frozen analysis plan

### 1. Cohort and split

- Freeze wallet membership before the analyzed period. Select on a documented
  pre-period activity rule, not same-period PnL, future activity, response to a
  candidate feature, or survivorship.
- Treat all historical/current artifacts as discovery and parser-calibration
  material. Start the untouched holdout only after this specification and any
  discovery choices are frozen and hashed.
- Fix the holdout duration from a discovery-period power calculation using the
  variance and count of independent market/shock clusters. Do not inspect
  partial holdout results to decide when to stop.

### 2. Event construction

- Resolve each BTC five-minute market and retain fills only in
  `[window_start, window_start + 300)`.
- For each wallet-market, group every fill in its earliest block into one
  episode. Preserve maker and taker episodes separately. If both outcomes occur
  in that block, report the episode as neutral/paired and exclude it from a
  directional first-leg test; do not choose whichever leg supports the signal.
- Define direction as the signed share or notional imbalance across the two
  outcomes in that earliest block. Predeclare the definition once.
- Treat taker first fills as interval-censored action proxies. Treat maker first
  fills only as passive execution/adverse-selection observations.

### 3. Causal ordering safeguards

- Use external source/publisher timestamps to define the economic observation
  and same-host monotonic receive timestamps to audit transport order. Never
  compare monotonic clocks across machines.
- Because the wallet timestamp is a whole-second block timestamp after
  off-chain matching, use predeclared safety margins of 5, 10, and 20 seconds.
  The external feature must use only complete observations ending strictly
  before `block_second - margin`.
- Measure external signed log returns over fixed 5, 10, and 30 second horizons
  for Binance spot, Binance futures, Deribit index/perpetual, and RTDS TWAP60.
  Start with continuous returns. Do not tune a shock threshold.
- Define the Polymarket pre-move from processed CLOB state before the fill block
  and the post-event markout from processed state beginning no earlier than
  `block_second + margin`, again at fixed 5, 10, and 30 second horizons. Use the
  Up-token midpoint or logit with a frozen crossed/empty-book rule.
- Exclude an observation if any required interval crosses a disconnect, raw or
  processed loss, clock anomaly, missing book state, unresolved token mapping,
  or source gap. Report each exclusion reason.
- A sequence qualifies only as `external_move -> observed_fill_block -> later_PM_move`
  under those margins. Even then the label is temporal association, not causal
  or subsecond reaction.

### 4. Outcomes and controls

Primary descriptive outcomes:

- signed first-block side alignment with each lagged external return;
- signed Polymarket 5/10/30-second markout after the fill block;
- whether the external return has incremental association with that markout
  after conditioning on the pre-fill Polymarket move;
- for the paired-inventory hypothesis, second-leg completion probability,
  completion delay, pair surplus, and residual-loss markout after a first fill.

Controls must be sampled in the same market, role, time-to-close bucket, spread,
depth, midpoint, price-to-beat distance, and volatility regime. Use both
activity-matched non-cohort wallets where available and pseudo-event seconds
from the same market. Give every common shock/market window one independent
weight; hundreds of wallets exposed to the same move are not hundreds of
independent signal observations.

### 5. Discovery, holdout, and inference

- Discovery may compare the predeclared continuous feature families and expose
  data-quality failures. Any chosen feature, sign convention, model, and
  threshold must then be frozen before opening the holdout.
- The holdout is evaluated once. Report every predeclared cell, including null
  and adverse results.
- Use two-way clustered uncertainty at wallet and market-window, with a UTC-day
  block bootstrap as a dependence check. If either dimension has fewer than 30
  independent clusters, or the day bootstrap is unsupported by enough complete
  days, label estimates descriptive and omit confirmatory p-values.
- Correct for the predeclared family of source/margin/horizon comparisons. Do
  not present the best cell as though it were the only test.
- Promotion requires prospective net economics after fees, fillability, and
  residue—not statistical alignment alone.

## Deterministic output contract

The eventual JSON report should contain:

- schema/version, immutable code/model identity, all parameters, and the exact
  frozen hypothesis list;
- SHA-256 and relative path for the join manifest, all source/raw/processed
  manifests, wallet-fill extract, market mapping, and Gamma labels;
- capture/clock/gap audit per source and explicit cross-binding checks;
- counts of wallets, markets, first-fill episodes, distinct shock clusters, and
  complete UTC days by split and maker/taker role;
- neutral/paired episodes and exclusions by reason;
- alignment, pre-move, post-markout, incremental association, and paired-
  completion summaries for every predeclared cell and matched control;
- clustered intervals only when independent-cluster gates are met;
- the exact interval-censoring statement below; and
- `order_timing_identified: false`, `causal_effect_identified: false`,
  `subsecond_reaction_identified: false`, and `strategy_validated: false`.

Required caveat:

> Winner events are first observed on-chain fill blocks. Polygon block
> timestamps are whole-second settlement times after off-chain matching and do
> not reveal order placement. Maker fills may come from orders resting before
> the measured shock. Safety margins establish only coarse temporal separation;
> reported associations are neither causal nor subsecond reaction estimates.

## Current Gen74/Gen75 collection gap

The concurrently running `gen74-fillprobe-20260825T2340Z` paper capture and
active Gen75 cross-venue capture already collect the core feeds needed for the
coarse study. Gen74 preserves the raw and actually processed Polymarket
book/`price_change` timeline with exact handler wall and monotonic timestamps;
Gen75 preserves RTDS TWAP60, Binance spot/futures, and Deribit raw frames with
receive and published-source timestamps. **No additional live feed is
required.** Their usable overlap begins at the Gen74 start and ends at the
earlier finalized run end.

Finalization and copying do not by themselves make the runs one dataset. The
offline release gate still requires:

- the complete Gen74 dataset, event file, raw/processed manifests, and every
  referenced chunk, plus the complete Gen75 dataset, telemetry, all four source
  manifests, and every referenced chunk—not review manifests alone;
- one immutable join manifest containing both dataset hashes, labels, exact
  overlap, frozen cohort hash, market/Gamma/fill-extract hashes, analysis-code
  identity, and validation results;
- common-clock evidence derived from all four run-boundary wall/monotonic
  anchors. Gen74 lacks clock-domain identity while Gen75 has explicit host/boot
  identity, and the captures still have no shared run ID or reciprocal hashes;
- zero-loss/cap/error and accepted-equals-written checks, verified source finals,
  a non-`unknown` Gen75 revision, and explicit reconnect/gap exclusion intervals;
- a deterministic V2-normalized selected-wallet fill extract for the overlap
  with wallet, slug/token/side, block/log, whole-second block time, maker/taker,
  size, price, fee, and preferably transaction hash; pre-period-matched control
  fills if wallet controls are used; and
- hashed resolved-market mappings and Gamma `priceToBeat`, `finalPrice`, config
  ID, and TWAP lookback.

Future local captures now stamp hashed host/boot clock identity and exact
cross-venue source lifecycle times, and require an explicit non-placeholder
revision. `tools/crossvenue_join.py` binds the finalized trees and passive
extracts offline without altering the active runs. For the active mixed Gen74/75
pair it binds Gen75's explicit identity but labels the relationship inferred,
requires all four wall-minus-monotonic anchors to agree within 50 ms, rejects
any Gen74 disconnect or connection failure, and validates every exact Gen75
connection/gap marker. It also retains a stricter both-legacy mode that rejects
unclocked paper or cross-venue gaps.
Its Gamma artifact is per-market and must bind `resolution_source`, lookback,
config, opening, and final values for exactly the resolved market set; it never
assumes one TWAP regime across historical and current windows.
The join also validates the frozen cohort addresses and pre-period boundary,
requires nonempty canonical V2 fill rows, rejects fills outside that cohort or
the exact complete-overlap market/token mapping, and reports zero-fill wallets
or markets as inactive rather than treating them as missing evidence.

These gaps can be closed offline after collection. The permanent limit remains:
public winner fills cannot reveal another wallet's order-placement time, so the
event must remain `first_observed_fill_block`, with maker fills treated as
passive executions that may originate from pre-shock resting orders.

## Release gate

Implementation is justified only after one complete overlapping prospective
dataset is present locally and passes all hashes, raw/processed count checks,
disconnect/gap audits, token mappings, and top-level cross-binding. Until then,
the correct deliverable is this gap report. Running the August 23 partial study
would spend analysis time while leaving every load-bearing timing ambiguity in
place.
