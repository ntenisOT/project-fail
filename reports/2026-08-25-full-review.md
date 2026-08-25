# Project Fail full review — 2026-08-25

## Verdict

**NO-GO for strategy promotion.** The project is now substantially better at
rejecting false strategies, but it does not yet reproduce a winning Polymarket
maker. The user reports being physically in Cyprus, which is not listed as a
restricted jurisdiction; nevertheless, neither currently tested source IP gives
us an unambiguous execution path, and no authenticated POST/cancel sample has
yet been run.

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

### 1. Eligibility and server geography were being conflated — open blocker

The user reports being physically in Cyprus, not London. Cyprus is not on the
current restricted-country list. However, Polymarket's live geoblock endpoint
returned `blocked=true` from the tested local route (`GB`) and Ireland server
(`IE`). The official pages conflict: the developer geoblock page labels Ireland
frontend-only/API-allowed and recommends `eu-west-1`, while the Help Center
lists Ireland as fully blocked ([developer geoblock](https://docs.polymarket.com/api-reference/geoblock),
[Help Center restrictions](https://help.polymarket.com/en/articles/13364163-geographic-restrictions)).
`DEPLOY_REGION` is not proof of the user's location or eligibility. Place paths
therefore remain disabled until this conflict is resolved without routing around
a restriction.

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

### 4. The clean accumulator used basket economics, but is regime-dependent

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

A fresh 03:50–09:45 UTC BTC cohort prevents us from turning that historical
mechanic into a permanent winner label. `0xb27…` retained 96.6% completion,
99.3% maker flow and 12 s / 57 s FIFO d50/d90, but its average pair cost rose to
$1.015 and terminal edge was -$4,292. Meanwhile `0x0ca…` earned +$2,040 with
only 22.1% maker/maker pairing, 83.6% completion and 7,997 residual shares. The
current winners are not a stable pure-inventory cohort; laddering needs a regime
gate and must be evaluated on neutral/adverse economics, not copied by identity.

### 5. Our execution topology is not a scaled-down winner — open blocker

The current engine holds one five-share order per outcome and stops near 20
shares. The clean leader repeatedly replenishes a broad price distribution and
averages thousands of paired shares per BTC market. It acquires across the full
$0–$1 range and completes most inventory within seconds.

Changing `$0.98` to `$0.985` cannot bridge this gap. A separate ladder and
replenishment engine is required; adding it to the already large pair engine
would be poor design.

### 6. One second is not the normal latency; tails are the real problem

Read-only Ireland measurements:

- REST `/time`: 27–32 ms median and 31–70 ms p90 across focused probes.
- Ordinary market-WebSocket event age: 8–13 ms p50 and 9–24 ms p90 in
  healthy intervals.
- Separate feed-tail incidents reached roughly 0.4–3.8 seconds and sometimes
  affected top-of-book or trade events.
- The configured 65 ms paper action delay is a sensitivity proxy, not a
  measured authenticated POST/cancel distribution.

These event types and timestamps are defined by the [official market-channel
protocol](https://docs.polymarket.com/api-reference/wss/market).

Therefore, a blanket one-second execution assumption materially overstates the
ordinary path. Pretending the tail does not exist would be equally wrong. The
remaining missing measurement is a real signed, post-only order acknowledgement
followed by targeted cancellation. A dry-run-safe, hard-capped probe now exists;
the user must run its money-moving mode under the standing launch boundary.

### 7. Paper used to censor feed-tail risk — fixed prospectively

Paper now:

- Pauses until both token feeds are causally fresh.
- Starts pairs no earlier than T+30 seconds.
- Freezes new decisions after a causal market event over 400 ms old, but keeps
  already-resting orders and delayed ordered trades exposed.
- Preserves event timestamps and rejects trades predating order activation.
- Reports `clean` and `lagged` economics separately; reconnects and unusably late
  first books remain invalid.
- Scores validity per strategy, not based on the first strategy in the cohort.

This avoids the flattering mistake of deleting exactly the windows in which a
live resting order would still be at risk. The earlier per-strategy settlement
bug is also fixed and covered by one focused test.

### 8. Current generation evidence is insufficient

- Gen38's apparent aggregate win was not BTC-driven; SOL/XRP concentration and
  residual direction dominated.
- Gen39–45 primarily found and fixed feed causality, settlement, and reporting
  errors. Those iterations improved the harness, not the trading edge.
- Gen46's first five resolved BTC windows rejected ten-share clips: the large
  maker arm ended with 27.2 unmatched shares and a -$4.91 adverse-outcome floor,
  versus 10 unmatched shares and a +$1.65 floor for its five-share control.
  The ten-share taker-completion arm lost $3.23.
- Gen46's five-share controls were positive over those five windows, but this is
  mechanism evidence only, not a profitability estimate.
- Gen47 then isolated the late-entry mechanism. In its first clean differentiated
  window, Basket99's fresh T+202 pair left directional residue (neutral -$0.56,
  adverse -$2.80); the T+180 cutoff stayed balanced at +$0.15. Taker completion
  improved that window to +$0.23 but paid $0.17 in fees, while its T+180 twin was
  identical to the maker cutoff. A following window was invalidated on a real
  WebSocket slow-consumer reconnect rather than silently scored.
- Gen48 immediately produced promising queue-aware mint and ladder fills, but a
  second `1013 slow consumer` disconnect invalidated the window at T+171. Gen49
  preserves the board but drains socket frames independently into a bounded,
  ordered queue; queue delay remains part of measured event age and overflow
  still fails closed.

A 15-minute generation is realistic for rejecting a mechanism or catching a
runtime defect. It is not realistic for estimating edge. Promotion requires at
least a 1–2 hour clean screen and a fresh roughly 21-hour/250-BTC-window cohort.

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

### 12. The separate mint EOA was an implementation shortcut, not a requirement

The earlier explanation that a Polymarket proxy "cannot mint" was wrong.
Polymarket's official Builder Relayer supports split, merge, redeem, and
approval transactions from a Safe/Proxy wallet, while that same wallet remains
the CLOB funder ([gasless transactions](https://docs.polymarket.com/trading/gasless),
[market-maker setup](https://docs.polymarket.com/market-makers/getting-started)).
The direct Polygon EOA existed because this repository had no Relayer
integration, not because the protocol forced a second inventory account.

The current balances expose the operational cost of that mistake: the CLOB Safe
has 9.855666 pUSD, while the legacy mint EOA holds 891.930536 USDC.e and zero
pUSD. Do not repair the direct-EOA place path. If direct minting becomes
evidence-backed again, use one Safe, one Relayer path, and one inventory ledger.
Moving or converting the legacy collateral is a separate user-authorized money
operation.

### 13. Mintbot is not strategy evidence

Mintbot is useful only as a shadow control-plane soak: feed baseline near 10
ms, about 12-second quote residence, 11 accumulated reconnects, and no real
orders or chain transactions. Its “minted/merged” rows are synthetic shadow
bookkeeping. It has no authenticated fill-price or ownership evidence and its
place path is hard-disabled.

### 14. Maintainability is mixed

The new forensic and settlement modules are bounded, and the focused test set
is fast. Three inherited files remain too large:

- `live/executor.py`: about 680 lines, dormant legacy path.
- `paper/pair_engine.py`: about 500 lines, active.
- `live/mintbot.py`: 501 lines, experimental.

Do not grow them. Retire the dormant executor unless a reviewed intent producer
returns. Implement ladders in a separate module and split stable inventory/order
state from orchestration only after the strategy survives.

## Current safety state

- Ireland: paper runner only plus shadow mintbot.
- No executor or lockbot process.
- All place paths hard-disabled in code.
- Legacy direct-EOA mint place path hard-disabled.
- CLOB Safe: 9.855666 pUSD; legacy mint EOA: 891.930536 USDC.e, zero pUSD.
- No order, cancellation, approval, or chain transaction was sent during this
  review.

## Focused verification

- 38 focused pair/ladder/mint/probe tests pass after the latest change.
- New exact FIFO wallet-pair test and existing winner/cycle tests pass.
- Ruff, compilation, and changed-file mypy checks pass (third-party imports are
  ignored locally because the CLOB V2 package is installed only on Ireland).
- Deployments were source-hash verified; unrelated files and secrets were not
  touched or printed.

## Next generation — precise scope

1. Archive Gen47 and run Gen48 with Basket98, unchanged Basket99, the
   mechanistically favored T+180 cutoff control, a separate stable two-level bid
   ladder, and one queue-aware mint-cycle control.
2. Compare ladder mechanics before PnL: residence, posts, FIFO completion,
   residual inventory, average pair, neutral PnL, and adverse floor.
3. Treat the mint arm as a falsification control. It earns continued development
   only if queue-aware paired asks produce positive neutral/adverse economics;
   synthetic mintbot rows do not count.
4. Target winner-shaped mechanics before PnL:
   - pair-completion coverage above 90%;
   - residual below 10% of acquired shares;
   - fee-inclusive average pair cost at most $0.99;
   - completion median 8–40 seconds and p90 at most 120 seconds;
   - positive neutral and adverse-outcome PnL;
   - no stale/reconnect-tainted scored exposure.
5. Keep signal skew disabled until an untouched cohort clears the five-cent
   gross-edge and lower-confidence-bound gate.
6. Measure one hard-capped authenticated POST/cancel round trip only under the
   user's explicit execution boundary; do not confuse that infrastructure probe
   with strategy promotion.

## Final assessment

We made enough progress for a first review because several false beliefs and
measurement bugs are now gone. We have **not** made enough progress to claim we
are close to the winners. The project has moved from an optimistic bot toward a
credible research harness. The next challenge is no longer “find the right
number”; it is to build the winner's laddered inventory machinery, prove its
mechanics without directional luck, and measure the authenticated path honestly.
