# Project Fail full review — 2026-08-25

## Verdict

**NO-GO for real money.** The project is now substantially better at rejecting
false strategies, but it does not yet reproduce a winning Polymarket maker and
cannot obtain compliant live-order evidence from its current locations.

The main progress was epistemic, not commercial: corrected CLOB V2 semantics
invalidated the old mint-and-ask story; exact FIFO reconstruction identified
rolling basket economics; latency was separated into a fast baseline and severe
public-feed tails; and paper windows now fail closed instead of reporting
contaminated fills.

## Scope reviewed

- Current repository architecture, active paper path, dormant live paths,
  mintbot, Polygon layer, ledgers, reports, risk guards, and focused tests.
- Ireland runtime processes, source hashes, logs, SQLite state, feed health, and
  the absence of executor/lockbot processes.
- Corrected ClickHouse winner normalization and bounded current/prior cohorts.
- Exact FIFO opposite-token acquisitions, ordered inventory cycles, timing,
  role, terminal markout, and public-tape signal persistence.
- Current official geographic restrictions, WebSocket protocol, fees, and
  maker-rebate rules.

## Critical findings

### 1. Current locations cannot place compliant orders — open blocker

Polymarket's live geoblock endpoint returned `blocked=true` from both the local
machine (`GB`) and Ireland server (`IE`). The current Help Center lists both
countries as blocked and prohibits proxy/VPN circumvention ([current geographic
restrictions](https://help.polymarket.com/en/articles/13364163-geographic-restrictions)). `DEPLOY_REGION` is
not an eligibility control. Documentation was corrected and all place paths
remain hard-disabled.

### 2. The original winner taxonomy was wrong — fixed

CLOB V2 emits one fill event per participating order. The taker summary uses an
exchange address as counterparty. Expanding every V2 row bilaterally fabricated
sells, maker roles, and buy/sell cycles. The corrected normalizer emits one user
leg per V2 row and retains bilateral expansion only for legacy events.

Consequently, trading both tokens does **not** prove minting or simultaneous
two-sided asks. Direct CTF split/merge activity is now reported separately and
zero direct activity is not treated as proof that a proxy did nothing.

### 3. Winning wallets are not one strategy

Corrected BTC sample, 2026-08-25 01:30–07:25 UTC:

| Wallet style | Trade PnL | Maker share | Exact paired-buy coverage | Exact average pair | Residual bought shares | Interpretation |
|---|---:|---:|---:|---:|---:|---|
| `0x0ca4…` | +$2,040 | 41.4% | 81.1% | $0.983 | 8,554 | Mixed paired inventory plus directional exposure |
| `0x408a…` | +$340 | 100% | 56.8% | $0.955 | 20,234 | Maker accumulator with extreme residual risk |
| `0xe114…` | +$420 | 100% | 79.1% | $0.994 | 13,866 | Maker accumulation plus profitable inventory cycles |

The headline leader's neutral inventory economics were approximately −$119;
about +$2,159 came from terminal direction. The clean churn wallet completed 35
ordered opposite-token cycles with about $514 cycle edge before rebates.

### 4. The clean accumulator uses basket economics — confirmed

For `0xb27…` across 253 BTC markets:

- 1,081,112 FIFO-paired shares, about 4,273 pairs per market.
- 97.3% of acquired shares were pair-completed.
- Exact fee-inclusive average pair cost: $0.982.
- Only 47.4% of paired shares individually cost at most $0.98; 61.3% cost at
  most $1.00.
- 98.2% of paired shares had both legs filled as maker.
- Completion delay: 8 seconds weighted median, 45 seconds p90.
- 60,227 residual bought shares across the cohort.

This rejects a hard cap on every individual pair. Cheap completed pairs fund
later balancing above the nominal cap. The rolling basket-cap implementation is
directionally correct.

### 5. Our execution topology is not a scaled-down winner — open blocker

The current engine holds one five-share order per outcome and stops near 20
shares. The clean leader repeatedly replenishes a broad price distribution and
averages thousands of paired shares per BTC market. It acquires across the full
$0–$1 range and completes most inventory within seconds.

Changing `$0.98` to `$0.985` cannot bridge this gap. A separate ladder and
replenishment engine is required; adding it to the already large pair engine
would be poor design.

### 6. Baseline latency is fast; public-feed tails are not — open blocker

Read-only Ireland measurements:

- REST `/time`: typically 27–34 ms median; observed p90 31–64 ms.
- Configured paper action proxy: 65 ms; twice-GET-p90 sensitivity ranged about
  65–130 ms. Authenticated POST/cancel remains unmeasured.
- Independent 60-second market-channel sample:
  - `best_bid_ask`: 11 ms median, 453 ms p90, 930 ms max.
  - `price_change`: 10 ms median, 363 ms p90, 978 ms max.
  - `last_trade_price`: 20 ms median, 509 ms p90, 972 ms max.

These event types and timestamps are defined by the [official market-channel
protocol](https://docs.polymarket.com/api-reference/wss/market).

Therefore, “one-second latency” is false as a blanket action assumption but real
as an intermittent public-feed tail. The tail reaches top-of-book and trades;
it is not just deep-book noise. Host CPU was 10–14% and NTP offset was only a
few microseconds during diagnosis.

### 7. Paper evidence was contaminated by feed tails — fixed prospectively

Paper now:

- Pauses until both token feeds are causally fresh.
- Starts pairs no earlier than T+30 seconds.
- Invalidates exposed strategy-windows after a causal market event over 400 ms
  old or a WebSocket reconnect.
- Preserves event timestamps and rejects trades predating order activation.
- Scores validity per strategy, not based on the first strategy in the cohort.

Gen41 exposed the prior settlement bug: Basket-$0.99 was feed-invalid while
stricter arms had not yet opened. The old loop would have settled every arm
using only the first arm's validity. This is fixed and covered by one focused
test.

### 8. Current generation evidence is insufficient

- Gen38 produced positive aggregate numbers, but BTC did not carry them;
  SOL/XRP concentration and residual direction dominated. The adverse-outcome
  metric rejected three of four variants.
- Gen39/40 were feed-tainted and are archived as invalid, not winners/losers.
- Gen41 produced one basket-$0.99 pair at $0.99 with a 29.6-second completion,
  matching current-wallet timing, then suffered a stale-feed event while
  exposed. Those fills are rejected.
- The only exposure-free valid Gen41 window found no qualifying opportunity.

A 15-minute generation is suitable for mechanism verification and rejection,
not statistical profitability. Promotion requires at least a 1–2 hour clean
screen and a fresh roughly 21-hour/250-BTC-window cohort.

### 9. No directional signal passes — rejected for now

The latest six-hour `0x0ca4…` cohort looked directionally profitable, but the
immediately preceding six hours had 41–49% correct calls before expiry and
negative directional dollars. The public-tape favorite also flipped sign
between regimes. This is not persistent out-of-sample evidence.

Minimum taker gate:

`lower_95(fair_value) - executable_price > fee + slippage + adverse-selection buffer`

Near 50 cents, the current crypto taker fee is 1.75 cents per share. With a
one-cent execution allowance and a safety/adverse-selection margin, about five
cents gross edge per share is a reasonable minimum screen. No tested public
signal meets it robustly.

### 10. Rebates are economically material but not deterministic

Current official rules: makers pay zero fees; crypto takers pay
`shares × 0.07 × p × (1-p)`; crypto maker rebates use a 20% fee-funded pool,
allocated by fee-equivalent share within each market and paid daily. The rate
may change ([fees](https://docs.polymarket.com/trading/fees), [maker rebates](https://docs.polymarket.com/programs/maker-rebates)).

Paper's `rebate$` is deliberately separate from PnL and labeled as a baseline
estimate. It must not be presented as actual payout. In the prior clean-leader
cohort, official endpoint payments materially exceeded the simple normalized
fill estimate, reinforcing the need to query actual rebates for wallet
comparisons.

### 11. Capital reporting understated resting collateral — fixed

Paper peak capital previously counted filled inventory but not collateral
reserved by resting bids. That inflated return on capital, including windows
with quotes but no fills. Resting bid notional is now included and covered by a
focused test. Old generation ROC figures remain stale and should not be used.

### 12. Mintbot is not strategy evidence

Mintbot is healthy only as a shadow control-plane soak: feed baseline near 10
ms, about 11.9-second quote residence, three historical reconnects, and no real
orders or chain transactions. Its “minted/merged” rows are synthetic shadow
bookkeeping. It has no authenticated fill-price or ownership evidence and its
place path is hard-disabled.

### 13. Maintainability is mixed

The new forensic and settlement modules are bounded, and the focused test set
is fast. Three inherited files remain too large:

- `live/executor.py`: 679 lines, dormant legacy path.
- `paper/pair_engine.py`: about 500 lines, active.
- `live/mintbot.py`: 501 lines, experimental.

Do not grow them. Retire the dormant executor unless a reviewed intent producer
returns. Implement ladders in a separate module and split stable inventory/order
state from orchestration only after the strategy survives.

## Current safety state

- Ireland: paper runner only plus shadow mintbot.
- No executor or lockbot process.
- All place paths hard-disabled in code.
- Direct CLOB V2 split/merge route hard-disabled.
- No order, cancellation, approval, or chain transaction was sent during this
  review.

## Focused verification

- 34 pair/feed/settlement tests pass after the latest fix.
- New exact FIFO wallet-pair test and existing winner/cycle tests pass.
- Ruff and changed-file mypy checks pass.
- Deployments were source-hash verified; unrelated files and secrets were not
  touched or printed.

## Next generation — precise scope

1. Deploy the per-strategy settlement and committed-capital fixes, archive the
   current DB, and restart at a clean boundary.
2. Keep the one-level basket-$0.99 arm as control.
3. Add a separate two-level maker-bid ladder with five-share clips, repeated
   replenishment, and a rolling basket cap. Do not add this to `pair_engine.py`.
4. Target winner-shaped mechanics before PnL:
   - pair-completion coverage above 90%;
   - residual below 10% of acquired shares;
   - fee-inclusive average pair cost at most $0.99;
   - completion median 8–40 seconds and p90 at most 120 seconds;
   - positive neutral and adverse-outcome PnL;
   - no stale/reconnect-tainted scored exposure.
5. Keep signal skew disabled until an untouched cohort clears the five-cent
   gross-edge and lower-confidence-bound gate.
6. Do not plan live money until both geographic eligibility and a materially
   better execution/feed path are independently proven.

## Final assessment

We made enough progress for a first review because several false beliefs and
measurement bugs are now gone. We have **not** made enough progress to claim we
are close to the winners. The project has moved from an optimistic bot toward a
credible research harness. The next challenge is no longer “find the right
number”; it is to build the winner's laddered inventory machinery and obtain a
feed/execution environment capable of testing it honestly.
