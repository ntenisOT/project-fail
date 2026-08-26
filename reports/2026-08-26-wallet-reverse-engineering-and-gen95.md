# Winner-wallet reverse engineering and Gen95 decision

Date: 2026-08-26

## Decision

The reported winners are real, but the original explanation was not. The
retail-scale winners do not fill before the five-minute event starts, and they
do not use the paired-maker mechanic carried by Basket99. They are directional
takers whose small positive selection edge disappears by the next public ask.

Do not copy their on-chain fills and do not promote a generic momentum or TWAP
arm. Those copies failed discovery/validation/holdout screens and a causal book
counterfactual. Gen95 therefore runs BTC-only observational paper with Basket99
as one control and `PAPER_LIVE_INTENTS=0`, while the independent cross-venue
collector continues. The purpose is to collect the synchronized pre-fill book
and reference state that public settlement blocks do not identify.

## Exact wallet re-score

The old leaderboard counted tokens as markets, truncated token lifecycles at
fill-day boundaries, and used a V2-unsafe maker-role rule. Re-scoring all 3,456
official Aug23-25 asset-windows with complete token lifecycles, repository V2
normalization, official outcomes, and explicit taker fees gives:

| wallet | net PnL | gross buys | return on buys | exact markets | maker share | result |
|---|---:|---:|---:|---:|---:|---|
| `0x20d2309c…e29d` | +$6,570.09 | $326,119.44 | 2.015% | 3,110 | 0.0% | verified |
| `0x5e2b9261…9101` | +$2,699.98 | $148,489.12 | 1.818% | 3,390 | 0.0% | verified |
| `0x75cc3b63…3ce1` | +$1,881.65 | $139,174.59 | 1.352% | 3,435 | 0.0% | verified |
| `0xb27bc932…5b82` | +$20,657.87 | $3,329,000 | 0.620% | 2,319 | 99.18% | verified, much larger scale |
| `0xae3…` | not attributable | — | — | — | — | 92,948 unexplained sold shares; inventory basis missing |

`winner_capital.py` does not measure capital. Its `max(bought_usd)` is
cumulative gross buys in one token, not peak concurrent collateral, and its V2
role expansion double-counts/mislabels trades. Exact peak capital remains
unresolved.

## The “before the window” claim

For `0x20d…`, `0x75…`, `0x5e…`, and `0xb27…`, in-event timing reconstruction
finds **0.0% pre-event fill volume**. The claim confused a different wallet's
CTF inventory operation with trading.

For `0x1dd2a69e…51c2`, 31 consecutive BTC windows show the actual mechanism:

- split 750 complete sets 188-288 seconds before the event (median 210 seconds);
- zero outcome-token sales before the event;
- first maker sale 3-45 seconds after start (median 7 seconds);
- last sale by second 298;
- merge leftovers around second 369-375 in 24/31 windows.

These markets and token IDs exist before the underlying price-measurement
interval. The wallet can therefore mint inventory and possibly rest orders
before T+0. Public fills do not reveal order submission, cancellation, queue
position, or unfilled quotes. The fresh 31-window accounting marks about
+$60.27 before final rebate attribution, but does not identify a replicable
quote policy or final capital requirement.

## What the directional takers do

`0x75…` and `0x5e…` are probably one bot family or a shared implementation:
their cumulative directions agree roughly 80-86%, 39% of grouped actions share
the exact block and side, and 53% have a same-market/same-side action within two
seconds. `0x20d…` is a separate strategy.

They are not paired arbitrageurs:

| wallet | FIFO paired completion | average fee-inclusive pair sum | pairs at or below $1 |
|---|---:|---:|---:|
| `0x20d…` | 59.7% | 1.023 | 43.0% |
| `0x5e…` | 57.9% | 1.035 | 40.1% |
| `0x75…` | 60.1% | 1.106 | 30.9% |
| `0xb27…` | 95.3% | 0.989 | — |

`0x75…` is buy-only in this period. Its BTC holdout made +$957.37, but the
best 5% of its windows contributed 95.5% of net profit; median window PnL was
+$3.15, worst -$58.34, best +$68.43. The bot generally buys a token as its
price rises: about 95% of actions were at the same or a higher price than the
strictly prior two-second print, and the share-weighted increase was about
7 cents. The apparent trigger cannot be inferred causally from Polygon block
time because off-chain matching precedes settlement.

## Falsification rounds

Every screen used Aug18-20 training, Aug21 validation where applicable, and an
untouched Aug23-25 holdout. Public trade prices are optimistic execution
proxies; adding real asks, delay, or slippage only makes the results worse.

1. Public favorite: 60-240 second favorite hit rates were 67-92%, but average
   PnL was around zero or negative after a 1-cent allowance plus fee.
2. Binance direction: fixed-time displacement/momentum grids produced positive
   discovery cells, then leading cells lost about 4-10 cents/share on
   validation/holdout.
3. Volatility fair value: a Brownian remaining-volatility model lost in both
   discovery and holdout at every predeclared cutoff/edge family.
4. Generic terminal CLOB momentum: signal at one print, wait two seconds, buy
   the next real taker-buy ask, then hold. Leading discovery cells failed
   validation and the three-day holdout; the frozen 10-cent/10-second rule lost
   roughly 2.4-4.1 cents/share depending on entry count.
5. Exact wallet follower: observe the selected wallet's block, buy the same
   side at the next real ask. Every wallet failed holdout after delay. Even
   `0x75…` actions of at least 20 shares produced only +0.27 cents/share before
   an added 1-cent execution allowance and -0.71 cents after it.

The decisive execution comparison is action-weighted and uses the winners'
exact selected sides:

| wallet | split | winner execution edge | next-ask edge | next ask worse by |
|---|---|---:|---:|---:|
| `0x75…` | discovery | +0.78c | -0.59c | 1.39c |
| `0x75…` | holdout | +0.27c | -1.20c | 1.49c |
| `0x5e…` | discovery | +1.50c | -0.30c | 1.81c |
| `0x5e…` | holdout | -0.01c | -1.60c | 1.60c |
| `0x20d…` | discovery | +1.60c | -1.08c | 2.69c |
| `0x20d…` | holdout | +1.27c | -1.00c | 2.29c |

The economic edge is first access to stale liquidity. An on-chain copy is
necessarily late.

## Gen94 adjudication

Gen94's four-asset feed received nine provider reconnects/`1013 slow consumer`
episodes. Its local queue was not saturated (HWM 343/8192; residence <=27 ms),
so all-assets upstream delivery was not decision-grade. The run ended naturally
through verified PID 4192329 and `paper/KILL`:

- raw frames: 2,791,263; raw bytes: 1,977,207,486;
- processed causal frames: 3,237,936;
- zero dropped frames, no cap, no writer error;
- both manifests, `run_end`, and dataset finalized;
- SQLite integrity: `ok`;
- archive DB SHA-256:
  `0342e8dd0c474fb777ceb09ce1fc252cbfb45a5d0dd7672167a7b3fa2db631b2`.

Replay found a separate defect: the live TWAP fill was driven by reference
updates applied outside the captured causal event stream. Replay therefore
missed that fill (1,455 replay fills vs 1,460 ledger fills). Gen94 is preserved
but cannot claim live/replay parity. Removing TWAP from the active board avoids
repeating this unbound input path.

The implemented `terminal10` counterfactual uses a 10-cent/10-second midpoint
move, 315 ms modeled taker delay, one-tick chase cap, two 5-share maximum clips,
explicit fees, and terminal settlement. On the immutable Gen94 capture it:

- emitted 16 clean BTC signals and filled all 16;
- scored eight BTC windows and invalidated eight reconnect/late-start windows;
- returned -$8.44 realized, -$13.44 neutral, and -$38.44 adverse floor.

It remains in the codebase for deterministic falsification but is deliberately
off the active board.

## Gen95 state and next evidence gate

Gen95 label: `gen95-btc-observe-20260826T2202Z`.

- paper assets: BTC only;
- board: behavior-preserved Basket99 control only;
- action/freshness: 65 ms / 400 ms;
- live intents: disabled;
- board SHA-256:
  `a9819cc55314019416de243c866da650f13a87face6658eab843f8ab4b09d9e5`;
- model SHA-256:
  `5de34394c6039df46bbc097e3dfbae0576934619354085a56011122edbd2b18d`;
- simultaneous independent cross-venue capture remains active and unchanged.

After a clean overlap is finalized, the next analysis must bind the paper CLOB
timeline, RTDS TWAP60, Binance spot/futures, Deribit, exact Gamma labels, and a
V2-normalized extract of these wallets. The event remains
`first_observed_fill_block`, never order time. Only pre-block external/book
state and post-block markout are identifiable. A tradable candidate must then
survive one frozen future holdout with displayed-depth execution and fees.

No place-mode component, real order, approval, collateral, or wallet state was
changed.
