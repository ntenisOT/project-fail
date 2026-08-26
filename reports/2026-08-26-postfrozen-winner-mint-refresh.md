# Post-frozen winner and mint-mechanism refresh — 2026-08-26

## Verdict

There is no strategy breakthrough. The fresh winner block rejects a private
late-directional-signal story, while the current paper probe still loses through
unmatched inventory. The useful breakthrough is forensic: the old query was
blind to explicit Safe/Relayer splits routed through Polymarket's current
collateral adapter. Minting is therefore **unresolved**, not disproven.

That correction does not revive the legacy mintbot. Most winner buys can already
be explained as CLOB BUY/BUY matches whose settlement atomically creates both
outcomes. This is paired-bid market structure, not proof that a wallet deliberately
minted a complete set and posted two asks. We must attribute adapter transfers to
recipient wallets before testing any mint-specific inventory or sell sequence.

## Immutable observation block

- Analysis code: `eed65db74f7b5a07ab82d45836e6ef33dce3c3be`.
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

## Ireland paper checkpoint at 00:39 UTC

Gen74 remained the same single BTC Basket99 fill probe with a 65 ms simulated
action delay, 400 ms stale-event cutoff, and 15-minute report cadence. SQLite
integrity was `ok`; capture accepted/written counts matched with zero drop, cap,
write error, future timestamp, or reconnect. No executor or mintbot process ran.

Ten scored windows plus one `late_first_books` invalidation produced 56 settled
maker trades and $89 volume:

| Metric | Gen74 checkpoint |
|---|---:|
| Realized PnL | -$4.99 |
| FIFO paired edge | +$4.12 |
| Neutral 50-cent inventory diagnostic | +$3.90 |
| Outcome component | -$8.90 |
| Adverse inventory floor | -$11.34 |
| Unmatched shares | 30.5 |
| Maker markout at 1 / 5 / 15 s | -1.00 / -1.38 / -1.80 cents/share |

All ten scored windows retained measured upstream-tail exposure. Simulated
activation was 76 ms; the loop stayed 1/1 ms p50/p90; local queue residence was
at most 33 ms lifetime and 3 ms in the latest interval. Upstream event age, not
the local loop, supplied the seconds-scale tail: 4.319 s lifetime. The official
T+30 reference sign matched only 3/10 winners and has no order path.

The 00:39 report task ran, but the notifier logs failures and disabled state—not
successful sends—so Telegram delivery remains unverified. The separate Gen75
passive capture recorded Deribit's clean 10-minute close and reconnect with exact
wall and monotonic times; the gap can now be excluded rather than hidden.

## Frozen-cohort result

Of 40 frozen wallets, 31 traded and 28 had FIFO pair evidence:

| Metric | Fresh value |
|---|---:|
| Descriptive actual PnL | -$9,590.65 |
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

### 2. Explicit Safe/Relayer lifecycle split or merge

The current standard adapter is
`0xAdA100Db00Ca00073811820692005400218FcE1f`. For an explicit split it pulls
pUSD from the caller, makes the adapter the raw CTF stakeholder, then transfers
both ERC-1155 outcomes adapter→caller in the same transaction. The reverse
transfer precedes an explicit merge. See Polymarket's
[position-management flow](https://docs.polymarket.com/trading/positions/manage)
and [current contracts](https://docs.polymarket.com/resources/contracts).

The fresh block contains 2,289 current-adapter splits for 414,832.029812 sets
and 762 merges for 176,331.833294 sets, with zero same-transaction CLOB maker
rows. These are a real non-CLOB-adjacent lifecycle population, but current
ClickHouse tables cannot assign them to winners:

- `splits_merges.stakeholder` is the adapter;
- the watchlist-filtered `erc1155_transfers` table covers 0/40 frozen wallets;
- `usdc_transfers` is empty and does not ingest pUSD;
- there is no transaction-call or trace table.

A raw receipt spot-check proves the blind spot: 100/100 outcomes were minted
zero→adapter, then transferred adapter→Safe in the same transaction. Consequently
“zero direct split at the trading address” means only “no same-address raw CTF
stakeholder event.” It cannot be used as “did not mint.”

The fail-closed receipt attributor is now implemented in
`tools/adapter_receipt_attributor.py` and `tools/adapter_receipt_core.py`. It
requires the exact condition token IDs, equal quantities, amount equality,
correct adapter↔wallet direction, and correct log order. Ambiguous transactions
remain unresolved, and no receipt RPC pass has yet been run. Only after that
bounded pass may we test whether explicit-minter wallets subsequently sell both
legs, merge inventory, or earn better neutral economics.

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

1. Keep Gen74 Basket99 as an engineering/fill probe only. It is not a promotion
   candidate while residue and adverse markouts persist.
2. Do not restart the legacy mintbot or rebuild the EOA path. First attribute
   explicit adapter lifecycle calls to wallets and test their economics.
3. Do not promote a late-directional arm. Current winner timing is explained by
   the already-public tape, and a cross-venue causal join is not yet finalized.
4. Keep the passive cross-venue capture. Exclude exact connection gaps and bind
   per-market Gamma regimes before any event-study claim.
5. Promote only one frozen, falsifiable candidate at a time after fee, queue,
   latency, residue, and untouched-holdout gates. More always-on arms would
   increase multiple-testing noise, not edge.
