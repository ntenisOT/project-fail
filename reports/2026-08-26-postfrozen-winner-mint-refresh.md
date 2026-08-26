# Post-frozen winner and mint-mechanism refresh — 2026-08-26

> **Economic interpretation superseded.** Receipt and rebate evidence below
> remains useful, but the 50-cent residual decomposition is not directional-signal
> evidence, the paired/residual lot allocation is not identified, and the exact
> fill-side ledger was not frozen. See
> `reports/2026-08-26-mint-to-make-hostile-review-adjudication.md`.

## Verdict

There is a mechanism breakthrough, not yet a strategy promotion. Receipt and
fill attribution show that one frozen wallet, `0x1dd2…51c2`, explicitly split
750 complete sets before every one of 31 windows, sold both outcomes only as
maker, and later merged in 24 windows. An integer-only rerun corroborated that
each merge equaled the smaller side of the observed split-minus-CLOB-fill
ledger, but that ledger is not yet hash-bound. The official rebate endpoint
also records $29.869474 for those exact conditions. This is the first defensible
evidence for the mint-to-make inventory mechanism that the original V1 analysis
claimed too early.

Fresh settlement-marked PnL is +$60.265 before the separately recorded rebate.
A chronological FIFO diagnostic assigns +$57.362 to paired-sale surplus and
only +$2.904 to price-relative residual performance. Public fills do not
identify the intended lot allocation, complete wallet balance, quote policy,
capital draw, or cash finality; the roughly -$70 zero-payoff floor after the
rebate remains a tail scenario, not a central cash estimate. The earlier frozen
seven-day artifact is now discovery for the corrected statistic and lacks
receipt and exact-rebate attribution. It is a style-persistence clue, not an
out-of-sample profitability result. The current Basket99 probe remains
materially worse because it accumulates unmatched inventory instead of starting
with complete sets and recycling symmetric leftovers. The next authorized step
is one exact historical accounting ledger, not a new paper arm, a repair of the
legacy EOA mintbot, or live orders.

## Immutable observation block

- Frozen winner aggregate code: `eed65db74f7b5a07ab82d45836e6ef33dce3c3be`.
- Receipt producer/attributor and strict artifact gates:
  `8d1cedca3c1cb160e565494b450a5e069950558b`.
- Fail-closed maker-rebate collector:
  `bab8b113505ce2463fe0a90b9ef4dd85d4a21f94`.
- BTC windows: 31 consecutive resolved markets, 2026-08-25 20:40 through
  23:10 UTC, epochs `1787690400..1787699400`.
- Outcomes: 15 Up / 16 Down.
- Lifecycle query: 2026-08-24 18:40 through 2026-08-26 00:10 UTC.
- ClickHouse watermarks at the audit: trades through 00:13:42, splits/merges
  through 00:13:42, redemptions through 00:13:40 UTC.
- Normalized input: 161,342 CLOB fill rows, 34,289 wallet-token aggregates,
  367 CTF stakeholder/market rows, and 3,622 wallets.
- The existing 40-wallet cohort was frozen before this block. No wallet was
  selected for promotion from these same 31 windows.

The rolling 288-window day is not fresh validation: 257 of its 288 BTC windows
overlap the frozen Gen73 period. Wallet sums below also share the same 31 market
shocks, so 31 active wallets are not 31 independent experiments.

## Ireland Gen74 final and Gen76 clocked restart

Gen74 finished as the same single BTC Basket99 fill probe with a 65 ms maker-action
proxy, 400 ms stale-event cutoff, and 15-minute report cadence. Its archived
SQLite SHA-256 is
`a33e1be36d397eb541e7f3f1b35f390a904fac058710edc8d68ad9ede88f4a`
and integrity was `ok`; no executor or mintbot process ran. Seventeen scored
windows and two invalid windows (`late_first_books=1`, `ws_reconnect=1`) produced
97 settled maker trades and $152 volume:

| Metric | Gen74 final |
|---|---:|
| Realized PnL | -$8.16 |
| FIFO paired edge | +$7.23 |
| Neutral 50-cent inventory diagnostic | +$11.82 |
| Outcome component | -$19.99 |
| Adverse inventory floor | -$23.47 |
| Unmatched shares | 62.6 |
| Maker markout at 1 / 5 / 15 s | -0.40 / -1.26 / -0.62 cents/share |

The loop remained 1/1 ms p50/p90, measured activation was 75 ms, and local queue
residence peaked at 33 ms. Seconds-scale observations came from upstream event
age: the unconditional stale-event maximum was 4.513 s, while the maximum during
simulated exposure was 4.383 s. That resolves the apparent discrepancy between
runtime-health and economics reports and rejects the earlier one-second
local-latency assumption. Gen74 also suffered a real `1013 slow consumer`
reconnect, so the conservative causal join rejects its overlap with Gen75.

The old tmux-first stop procedure then exposed an output-integrity defect:
`tmux kill-session` delivered termination before Gen74 wrote `run_end`, raw
manifests, or its dataset JSON. Its DB/log are archived, but the raw capture is
not exact-replay evidence unless a separate audited recovery proves otherwise.
The README now requires a verified PID plus `paper/KILL`, natural exit,
finalized manifests and replay validation, and only then dead-session cleanup
and archival.

Gen76 started at 01:19:17 UTC with unchanged Basket99/BTC/65 ms/400 ms parameters.
Its explicit host/boot clock identity exactly matches the still-running Gen75
cross-venue capture; the first full 01:20 window opened on time. At the 01:37:39
checkpoint, its second report followed the first by 900.156 seconds and Telegram
acknowledged it 259 ms later. That proves configured cadence and API acceptance,
not handset delivery. Raw and causal writers reconciled exactly with zero
drop/cap/error/reconnect and SQLite remained `ok`. Feed age was 13/116 ms p50/p90,
but the lifetime tail reached 2.298 s and 23,041 stale events plus 517 delayed
trades had been counted; the 400 ms guard is excluding real bursts, not describing
a 400 ms-clean feed. Only two windows were scored: +$2.18 neutral became -$1.88
actual through -$4.06 of outcome exposure, so this tiny checkpoint is consistent
with the rejected residue mechanism but cannot estimate strategy performance.
Gen75's process was not restarted, and no live path exists.

Gen75 continued to grow with zero source drop/cap/error. Its seven closed
connections were clean Deribit ten-minute cycles with exact excluded gaps from
184 ms to 5.113 s. Observed source-age p50/p90 was approximately 106/112 ms for
Binance futures, 110/117 ms for Binance spot, 114/551 ms for Deribit, and
1,723/2,205 ms for Polymarket RTDS. RTDS is the official-reference shadow, not a
subsecond leading signal. Final dataset acceptance still requires all in-flight
writer counts to reconcile and every source gap to be excluded.

## Frozen-cohort result

Of 40 frozen wallets, 31 traded and 28 had FIFO pair evidence:

| Metric | Fresh value |
|---|---:|
| Descriptive settlement-marked PnL | -$9,590.65 |
| Outcome-neutral PnL | -$71,977.10 |
| Directional component | +$62,386.46 |
| Volume | $635,844 |
| Explicit taker fees | $4,518 |
| Exact aggregate FIFO pair sum | 1.027159 |
| FIFO completion | 73.35% |
| Actual-positive active wallets | 15 / 31 |
| Neutral-positive active wallets | 12 / 31 |

The two predeclared neutral pair exemplars did not validate profit persistence:

| Wallet | Fresh behavior | FIFO sum | Completion | Neutral | Actual |
|---|---|---:|---:|---:|---:|
| `0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82` | inactive | — | — | $0 | $0 |
| `0x3048d65321be3497164cdfc2996f94f98a2e7537` | all 31 markets | 0.984147 | 91.45% | +$390 | +$27 |

`0x3048…` preserved cheap-pair acquisition, but residual direction erased almost
all of its neutral gain. The largest same-period PnL wallet,
`0xcb92e59eef071c7dfac33fbc64c60952d277e62f`, was not a pair candidate:
0% maker volume, 1.3379 pair proxy, 66.3% unexplained sold-inventory floor,
neutral -$312, directional +$1,987, actual +$1,675. Re-ranking and promoting
other cheap-pair wallets from this block would be selection on the test period.

## No identified private late signal

The public Polymarket last-trade favorite was already correct in 30/31 markets
at final-60 seconds and 31/31 at final-30 and final-10. Its average favorite
price was $0.920, $0.958, and $0.987 respectively. The most directional frozen
wallets agreed with that public tape roughly 98–100% of the time; some paid about
$0.94 in the final ten seconds.

Therefore late outcome alignment is mostly a public, nearly resolved market
fact. Polygon block time is later than off-chain matching and cannot identify
maker order-placement time or subsecond reaction. No current dataset supports a
private signal, a causal response to Binance/Deribit, or a profitable threshold.

The settlement-manipulation result in
[arXiv 2606.31675](https://arxiv.org/abs/2606.31675) remains a discovery
hypothesis only: its February–April sample predates Polymarket's current TWAP
regime. Exact Gamma metadata shows the five-minute BTC source changed from a
30-second to a 60-second TWAP between 2026-08-13 23:55 and 2026-08-14 00:00 UTC.
Historical joins must bind each market's own `resolutionSource`.

## The mint attribution correction

Two adapter mechanisms had been conflated.

### 1. CLOB atomic settlement

The old adapter `0xADa100874d00e3331D00F2007a9c336a65009718` is the CLOB V2
`outcomeTokenFactory`. A BUY/BUY match calls `splitPosition`; a SELL/SELL match
calls `mergePositions`. This is exchange settlement, not a deliberate Safe
lifecycle call by either wallet.

In the fresh block, 39,057 of 39,140 old-adapter split operations joined a
same-transaction CLOB operation with exact set/share equality. Among the 30
frozen wallets with buys, every wallet had atomic-mint-settled buys:
903,317.290266 of 1,060,838.896715 shares, or **85.15%**. The per-wallet median
was 85.90% (72.42–99.81%). Earlier frozen blocks were similar at 87–90%.

This strongly supports complementary bids being matched through the engine. It
does not show that either wallet received both outcomes.

### 2. Explicit position-adapter counterparty split or merge

The current standard adapter is
`0xAdA100Db00Ca00073811820692005400218FcE1f`. For an explicit split it pulls
pUSD from the caller, makes the adapter the raw CTF stakeholder, then transfers
both ERC-1155 outcomes adapter→caller in the same transaction. The reverse
transfer precedes an explicit merge. See Polymarket's
[position-management flow](https://docs.polymarket.com/trading/positions/manage)
and [current contracts](https://docs.polymarket.com/resources/contracts).

The fresh block contains 2,289 current-adapter splits for 414,832.029812 sets
and 762 merges for 176,331.833294 sets, with zero same-transaction CLOB maker
rows. These are a real non-CLOB-adjacent lifecycle population. ClickHouse alone
cannot assign them to winners:

- `splits_merges.stakeholder` is the adapter;
- the watchlist-filtered `erc1155_transfers` table covers 0/40 frozen wallets;
- `usdc_transfers` is empty and does not ingest pUSD;
- there is no transaction-call or trace table.

The fail-closed receipt pass resolved all 3,180 bounded candidates with exact
condition token IDs, amount equality, transfer direction, log order, source
block, embedded-query hash, and mapping hash. All were explicit adapter-wallet
transfers; none was unresolved. Only three receipt counterparties intersected
the frozen 40-wallet cohort:

| Wallet | Proven lifecycle | Correct interpretation |
|---|---|---|
| `0x1dd2…51c2` | 31 × 750-set splits; 24 merges totaling 12,695.929028 sets | mint-funded two-sided maker seller |
| `0x9d57…56ea` | 13 × 100-set splits; 53 merges totaling 8,176.873476 sets | buy-heavy accumulator and merge recycler |
| `0x335a…a494` | 31 × 500-set splits; no merge | inventory provisioning with no same-address CLOB monetization |

`0x1dd2…` is the clean mechanism. Every split occurred 188–288 seconds before
the event start. The first settled fills arrived 3–45 seconds after start; all
1,628 owner legs and $6,234.02 book volume were maker sells, with both outcomes
sold in 31/31 windows and zero CLOB buys. Each of 24 terminal merges occurred at
event +369..+375 seconds, after the last fill, and equaled the exact smaller side
of the observed split-minus-CLOB-fill ledger. Seven windows did not merge.
On-chain order is exact, but it cannot reveal complete wallet balances,
pre-split order submission, or unfilled/cancelled quotes.

The original proportional allocation reported +$17.896 on a 50-cent residual
diagnostic and +$60.265 settlement-marked, assigning +$27.188 to 6,098.124
paired sold sets and +$33.077 to roughly 236 excess sold shares. That allocation
is not identified by public fills. A later chronological FIFO audit assigned
+$57.362 to paired-sale surplus and only +$2.904 to price-relative residual
performance; admissible excess-fill allocations span roughly -$43 to +$101.
The total is invariant, but the narrative is not, and none of these fill-side
economics is yet in a frozen artifact. The public Polymarket
[maker-rebate endpoint](https://docs.polymarket.com/api-reference/rebates/get-current-rebated-fees-for-a-maker)
returned one row for every exact condition, totaling $29.869474. Including that
endpoint-recorded rebate amount gives approximately +$47.77 on the 50-cent
diagnostic and +$90.13 settlement-marked, before any unobserved Relayer/builder
cost or proof of cash-settlement timing.

The +$47.77 number is an obsolete 50-cent diagnostic, not an outcome-neutral
cash result. Valuing the unmatched shares at zero puts the adverse residual
floor near -$70.2, including the rebate and before omitted costs. Any replication
must report redemption, executable flattening, zero-payoff floor, and cash
finality separately. The relevant question is whether paired-sale surplus plus
defensible rebates survives queue loss, costs, capital, and tail-risk limits—not
whether the 50-cent diagnostic clears a threshold.

The same endpoint returned $130.252864 for `0x9d57…`'s 30 active conditions.
Its legacy 50-cent decomposition is not used for a signal claim. Its 43,068.036
FIFO buy pairs averaged a fee-inclusive 1.006294 and lost $271.080; profitable
same-token unwinds and rebates, not cheap pair entry alone, supplied its reported
inventory edge. That is a different mechanism requiring its own exact benchmark
audit. `0x335a…` had no rebate for these conditions and no same-address CLOB rows.

The already-frozen seven-day artifact ending before this fresh block (SHA-256
`ecd98ac5d03427e26f9324b0327c6b2f0f73539d4e62f50773cd1f8552ae0825`)
contains an important independent style clue for `0x1dd2…`. Its discovery slice
has 1,395 markets, $359,792.53 volume, 100% maker volume, +$978.83
settlement-marked PnL, and a -$867.16 50-cent diagnostic. Its later slice has 494
markets, $106,171.10 volume, 100% maker volume, +$544.40 settlement-marked PnL,
and a +$95.88 50-cent diagnostic. The diagnostics do not establish neutral
economics; the later raw FIFO audit is summarized in the adjudication report.
The artifact predates the adapter correction and cannot prove those earlier
sells were funded by 750-set splits. Exact wallet-specific receipt attribution,
condition-matched daily rebates, and capital cashflow across this frozen period
are the next required persistence test; extrapolating the fresh rebate rate
would be data substitution, not validation.

Receipt artifact SHA-256:
`4c37c45aaffeb925e72dd6aab1ef0985b8a9eeaa81f0d397d3fc1898d4363f31`.
Rebate evidence SHA-256 is
`c2a2d60e909ee4da0e21e13f2cb58c4880fd48715ea0910b277c5cf8734f753d`
for `0x1dd2…` (31 selected, 251 unrelated response conditions excluded) and
`586832f28481ed4abc9a52e8be52919817d4cc151595c5f42b3c0a0a215ca947`
for `0x9d57…` (30 selected, 627 unrelated response conditions excluded, one
mapped inactive condition absent). Both bind collector revision
`bab8b113505ce2463fe0a90b9ef4dd85d4a21f94` and the source mapping artifact.
Attribution proves the adapter counterparty, not beneficial ownership, pUSD cash
timing, downstream transfers, or off-chain quote lifecycle.

### What a valid mint replay must do

The fills identify a lifecycle, not a quote policy. They do not reveal submission
time, continuous presence, size schedule, cancellation, repricing, or queue
priority. Seeding a simulator with `0x1dd2…`'s observed fills would assume the
missing execution and manufacture the answer. Two separate replays are required:

1. An exact historical accounting replay must conserve receipt-attributed token
   and pUSD flows across splits, fills, transfers, merges, redemption, and
   condition-matched rebates. It makes no counterfactual fill claim.
2. A causal policy replay must generate orders from one immutable manifest
   against raw books/trades, with no winner-fill seeding. It must model displayed
   queue, maker activation/cancel acknowledgement, late fills, exact base-unit
   merge, and residual liquidation/hold policies.

The primary strategy gate excludes projected rebates and requires positive
economics under a genuine outcome-neutral residual policy. Recorded rebates are
an overlay because the pool is discretionary and unavailable for immediate
capital reuse. A mechanism-faithful hold, an executable neutral flatten, and an
operational-failure case must all be reported; the seven no-merge windows cannot
be silently converted into successful merges.

Capital is also unresolved. A $750 split is not the capital denominator because
the next split occurred before the preceding T+369..375-second merge, and seven
tails did not merge. The replay must calculate peak confirmed cash draw,
simultaneously open principals, capital-seconds, and return on peak cash without
backdating sale, merge, redemption, or rebate availability.

## Completion opportunity census

The rejected immediate-completion policy is now supplemented by an observation
census that does not apply a hypothetical action or calculate policy PnL.

- Prospective tape: 10 first-leg episodes; four natural maker completions, five
  censored episodes, and one fee-inclusive taker opportunity. The sole opportunity
  was open at capture stop. There were **zero in resolved prospective windows**.
- Calibration tape: seven opportunities; four resolved, two finished but
  unresolved, one open at stop. This is diagnosis on a reused tape, not validation.
- Two negative maker-reference spread costs mean a simulated resting bid could
  have been crossed by the later displayed ask. They are not observed fills.

Artifacts:

- prospective: `77a50668029e0b34f0e0d1bba433b149e8cb05e8a7c0c0c83f9321722416922e`;
- calibration: `2fac6f14c96e78448a2c8cd55d40cfb1a86333be4c318a4762cb45e3c6c159b6`.

This closes the obvious “just complete immediately” shortcut. The next pair
research must estimate a censored second-leg hazard and the value of stopping
new first-leg initiation—not retune another completion timer on the same tapes.

## External research prior

Current protocol economics are now verified rather than assumed. Official
[fee documentation](https://docs.polymarket.com/trading/fees) keeps crypto at
`0.07 × shares × p × (1-p)`, zero maker fee, and a 20% fee-curve-weighted maker
rebate. The program pays daily and the percentage remains discretionary. The
current BTC market-info response returned `fd.r=0.07`, `fd.to=true`, minimum
order size five, and `itode=true`. The official
[market-info schema](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)
defines `itode` as a 250 ms hold for marketable taker orders. Therefore the
65 ms paper action proxy is maker-only; any future taker/signal replay must add
the 250 ms venue delay and query it per market.

The literature argues against a generic “add Binance features” strategy.
[OpenMarket](https://arxiv.org/abs/2607.26245) synchronized Polymarket and
Binance at millisecond scale; its 43-feature walk-forward model did not beat the
probability already in Polymarket's book and lost after its stated fee/slippage
model. It did find that Polymarket quotes responded after large Binance moves,
which supports measuring conditional latency but not assuming tradable alpha.

[Execution, not Information](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6191618)
separates side selection from price paid across a much larger resolved-trade
archive and finds profitability concentrated in execution rather than forecast
accuracy. [The Anatomy of a Decentralized Prediction Market](https://arxiv.org/abs/2604.24366)
also finds that trade direction inferred from the public order-book feed is only
slightly better than chance and documents multi-second ingestion tails; causal
microstructure work needs the authoritative settlement record.

These are priors, not portable estimates for current five-minute TWAP60 BTC.
They make our promotion hurdle stricter: a signal arm must add value over the
causal Polymarket book itself after fees, delay, and selection, while the
inventory arm must first survive fill degradation and incomplete-leg loss.

Polymarket's current [CTF Exchange V2 source](https://github.com/Polymarket/ctf-exchange-v2)
also removes an important ambiguity: `MINT` is the exchange settlement path for
two BUY orders and `MERGE` is the path for two SELL orders. A raw split/merge
event at the exchange adapter is therefore not evidence that a winner chose a
mint-inventory strategy. A recent [fill-side microstructure study](https://arxiv.org/abs/2605.11640)
reaches a compatible identification limit: because order placement and
cancellation are off-chain, public on-chain fills cannot reconstruct an
address's quote lifecycle or prove it was continuously two-sided.

Two tempting web directions do not clear an evidence gate. An NBA
[order-book arbitrage study](https://arxiv.org/abs/2605.00864) found only seven
single-market episodes across 3,042 markets and severe depth constraints; that
is a useful prior but not a BTC estimate. [PolySwarm](https://arxiv.org/abs/2604.03888)
describes a 50-agent forecasting and latency-arbitrage architecture, yet reports
probability-calibration comparisons rather than a fee- and fill-realistic
strategy PnL result. It is not evidence that adding LLM votes creates tradable
edge.

## Decisions

1. Reject Gen74 Basket99 economically and quarantine its unfinalized causal
   capture. Gen76 is only the clocked measurement continuation, not a strategy
   promotion.
2. Hostile review is complete. Advance exactly one experiment: an integer-exact,
   fail-closed accounting ledger for `0x1dd2…` across Fresh31 and the earlier
   seven-day period. Do not infer a quote policy or start replay/paper until that
   ledger passes conservation, provenance, capital, cash-state, and tail-risk
   gates. Do not repair the legacy EOA mintbot; any eventual chain path is Safe +
   Relayer + current pUSD adapter.
3. Keep `0x9d57…` separate. It is a buy/merge/unwind/rebate mechanism requiring
   its own price-relative benchmark, not evidence for the same mint arm.
4. Do not promote a late-directional arm or invent a signal threshold. Current
   winner timing is already public on the tape, `0x1dd2…` has only +$2.90 of
   post-hoc price-relative FIFO residual in the fresh block, and the cross-venue
   causal join is not finalized.
5. Keep Gen75 passive capture. Exclude exact connection gaps, bind each Gamma
   regime, and add the per-market 250 ms taker delay before any executable event
   study.
6. Promote only one frozen, falsifiable candidate at a time after actual rebate,
   fee, queue, maker-activation, inventory, merge, residue, capital-turnover, and
   untouched-holdout gates. No live or money path is authorized.
