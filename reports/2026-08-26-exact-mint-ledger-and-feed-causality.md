# Exact mint ledger and feed-causality decision — 2026-08-26

## Bottom line

The feed defect is fixed. The mint strategy is not proven profitable.

Revision `030f157c5ec2f912cde1a515de98c278077c9a3c` contains the final
feed-causality patch and the first frozen, integer-exact observed ledger for
`0x1dd2…51c2`. A short Ireland calibration exercised real stale-feed
pause/recovery, finalized without capture loss, and replayed with exact ledger
parity. That is an engineering GO for paper calibration and mint shadow only.
It is not an economic result and does not authorize orders.

The ledger proves a real mechanism: 31 standard-adapter splits of 750 complete
sets, 1,628 fee-zero V2 maker sells on both outcomes, zero buys, and 24 exact
terminal merges. It does not prove cash realization, quote policy, queue
priority, capital efficiency, or reproducible profit. The contractual terminal
mark is +$60.265463 before an endpoint-recorded $29.869474 rebate, while the
residual-zero-payoff case is -$100.107122 before the rebate. Observed event cash
flow is -$4,320.048803; the positive result appears only after marking
$4,380.314266 of retained winning tokens at par.

Both independent reviewers therefore return **NO-GO on implementation**, reject
another Basket99 variant as the wrong mechanism, and keep live/place disabled.
The correct sequence is:

1. repair the frozen artifact's provenance, winner, lifecycle-tail, capital and
   cash-state omissions without changing the economic hypothesis;
2. run exactly one pre-registered sibling-wallet falsification over the ten
   other frozen 31×750 addresses;
3. only if that survives, freeze one independently specified mint-to-make quote
   policy and test it prospectively on raw books with queue and capital realism.

Step 1 is a correction to incomplete evidence, not a strategy experiment. Step
2 is the one next experiment. It can falsify mint-to-make cheaply; because all
wallets share the same 31 market shocks, it cannot by itself establish
out-of-sample profitability.

## Immutable evidence

### Source and accounting artifact

- source revision: `030f157c5ec2f912cde1a515de98c278077c9a3c`;
- feed commit: `ddda045` (`Close market feed causal gaps`);
- accounting commit: `030f157` (`Freeze exact mint seller accounting`);
- artifact: `out/mint1dd2-fresh31-accounting-v1.json`;
- artifact SHA-256:
  `d0688e0aee9d39fb84f50a66d66725cf155138c1dc95f43a0ec13101fc21e1a8`;
- artifact basis: `observed_ledger_with_contractual_terminal_mark`;
- `complete_wallet=false`, `cash_realized=false`.

Pinned inputs:

| Input | SHA-256 |
|---|---|
| receipt candidates | `073886fe970707d23927dfcc30342b31eb7fc2c6cef5717d49b1535c374be651` |
| receipt attribution | `4c37c45aaffeb925e72dd6aab1ef0985b8a9eeaa81f0d397d3fc1898d4363f31` |
| receipt cache | `1457722847aabfcca193097e4378c7ce0dd9bc8c3e055d9955850a65bbac8fef` |
| condition rebates | `c2a2d60e909ee4da0e21e13f2cb58c4880fd48715ea0910b277c5cf8734f753d` |

### Ireland Gen78

- label: `gen78-feedfix-030f157-20260826T0323Z`;
- start: 2026-08-26 03:22:23 UTC;
- natural `paper/KILL` stop: 03:36:05 UTC;
- BTC only, Basket99 mechanics probe, 65 ms maker-action proxy, 400 ms
  source-age gate, 10 ms decision cadence;
- two complete 5-minute windows crossed cleanly, plus partial boundary windows;
- raw frames: 242,577; processed causal frames: 319,414;
- replay records: 7 fills, 1 invalid window, 0 settlements;
- exact live/replay parity for all 8 finalized records;
- reconnects 0, capture drops 0, cap false, writer errors 0;
- local loop p50/p90 1/1 ms, lifetime loop max 27 ms;
- queue residence max 5 ms;
- upstream source-age lifetime max 4.942 s;
- an actual stale-feed pause and authoritative recovery occurred;
- no executor, mintbot or lockbot process ran.

Archived evidence:

| File | SHA-256 |
|---|---|
| `out/gen78-paper.db` | `a1303b6a07e8d2b9cc9181d7100ea97a900e6db810a57ab052dc76cd00340fb5` |
| `out/gen78-paper.replay.json` | `4fa4e1a2780937fdb5f8122c96be2dad931dd78d2bb7881d2c343b3affd76035` |
| dataset manifest | `4bd342c87f07559944d5384957cc38f2a4b80c9caf261a39d92abee919bc6e38` |
| raw manifest | `65da883cf49e8618f86197ae6e0a5a032886c54e7eaf99d828b84513e45605c5` |
| causal manifest | `24a126c7463f3a8055cb9a7400e1dbbc3b0e4bacad233525ef6a930f4ab58153` |

Gen76 and Gen77 are not economic evidence. Gen76 ran with the stale-cache
causality defect. Gen77 was stopped after five minutes when hostile review found
the first patch incomplete. Gen78 validates repaired engineering behavior only;
two complete windows and no settlement cannot estimate profit.

## What the feed fix actually closes

The market stream now uses high-frequency `price_change` deltas as well as full
book snapshots. The repaired path:

- requires an authoritative snapshot before accepting a delta chain;
- invalidates bootstrap after a stale, future, malformed or disconnected chain;
- expires by exact source age, not a second receive-time TTL;
- rejects non-finite timestamps and numerics;
- fails closed on invalid JSON, non-object frames and structurally malformed
  mixed payloads;
- treats tick-size changes as metadata, not false depth refreshes;
- tracks per-token revisions so unrelated assets cannot validate a stale plan;
- rechecks a quote plan before placement, after cancel and after placement;
- targeted-cancels an order placed across an in-flight stale revision;
- clears and wakes/cancels before reconnect backoff.

Thirty focused regressions passed in 2.40 seconds. Ruff and mypy are clean on
the changed source. This is enough meaningful coverage for the patch; broad
test-count inflation would not add evidence.

The calibration also resolves the earlier latency confusion. Local processing
is about 1 ms, not one second. The seconds-scale problem is intermittent
upstream event age: Gen78 observed a 4.942-second tail and correctly stopped
acting on it. A single latency number is the wrong model; future reports must
separate source age, local queue residence, decision-loop time, maker action
proxy and the venue's separate taker delay.

## Exact Fresh31 ledger

| Observed item | Exact value |
|---|---:|
| markets / splits | 31 / 31 |
| split principal | $23,250.000000 |
| maker sells / buys | 1,628 / 0 |
| sale cash | $6,234.022169 |
| merges / merge return | 24 / $12,695.929028 |
| observed event cash flow | **-$4,320.048803** |
| contractual winning-token mark | $4,380.314266 |
| losing retained tokens marked zero | $4,295.574514 |
| contractual terminal PnL | **+$60.265463** |
| endpoint-recorded rebate | +$29.869474 |
| terminal plus endpoint rebate | **+$90.134937** |
| unmatched zero-payoff floor | **-$100.107122** |
| floor plus endpoint rebate | **-$70.237648** |

The arithmetic and lifecycle invariants survived both hostile reviews. All
money uses integer base units or exact rational arithmetic. The generator aborts
on a buy, taker fill, non-zero fee, neg-risk row, non-V2 exchange row, wrong
adapter, unmapped token, negative inventory, wrong merge amount, merge before
the last fill, duplicate event identity, allocation mismatch, stale source
watermark, changed input hash or output overwrite.

That exactness does not make the inference exact:

- FIFO assigns +$57.361797 to paired sales and +$2.903666 to residuals;
- proportional allocation assigns +$27.188431 and +$33.077032;
- admissible residual PnL spans -$43.006380 to +$100.685459.

The total terminal mark is invariant. The story about which lots earned it is
not identified by public fills.

The sample is also fragile:

- mean contractual PnL is $1.9440/window;
- standard deviation is $6.0849;
- naive one-sample t-statistic is 1.779;
- 9 of 31 windows lose;
- the best two windows contribute 55.12% of total profit;
- the best five contribute 97.48%;
- the remaining 26 windows contribute only +$1.52.

These are descriptive, not confirmatory statistics. The wallet was selected
after examining the same cohort and market period, and all wallets share market
shocks. Treating 31 wallets or fills as independent samples would be false
precision.

## What is proven and what is not

### Proven within the observed ledger

- the standard-adapter split → both-outcome maker-sell → terminal-merge
  mechanism exists for this address and period;
- every split was 750 sets and every observed CLOB fill was a fee-zero V2 maker
  sell;
- both outcomes sold in all 31 windows and no CLOB buy was observed;
- 24 merges equal the exact smaller remainder after observed sells and occur
  after the last fill;
- observed token and event-cash arithmetic conserves exactly;
- the endpoint records condition-matched rebate amounts totaling $29.869474.

### Not proven

- complete wallet custody or external ERC-1155 transfers;
- pUSD redemption cash or rebate payment finality;
- historical cash PnL or peak confirmed cash draw;
- simultaneous principals, capital-seconds or return on peak capital;
- why seven windows did not merge within the bounded lifecycle;
- submission, cancellation, repricing, unfilled quotes or continuous presence;
- queue position, fill degradation, scalability or our attainable spread;
- a private Binance, Deribit or late-outcome signal;
- out-of-sample persistence.

Current source coverage explains the limits. `erc1155_transfers` has 5,226 rows
in the interval but zero mapped-token and zero target-address rows;
`usdc_transfers` is globally empty; the target has zero pUSD redemption rows;
the six observed target redemptions are legacy-USDC events with payout zero.
The redemption table itself is not missing: it covers all 31 conditions and
pUSD activity by other addresses. What is missing is the attribution path from
the target or its proxy to settlement cash.

## Independent reviews

Both reviewers received the same packet in a disposable detached worktree at
exact revision `030f157`, with the artifact copied as read-only evidence. They
did not see each other's answer. The primary worktree stayed clean.

- Claude: exact `claude-opus-5`, effort `max`, plan permission, safe mode,
  `Read,Glob,Grep` tools only, no session persistence or fallback.
- Qwen: exact `qwen3.8-max`, safe mode, no sandbox fallback.

### Agreement

Both found no material arithmetic, lifecycle, SQL or conservation bug. Both
found the artifact materially incomplete as strategy evidence:

- source hashing omits imported `market_windows.py` and
  `clickhouse_forensics.py`;
- the database/gate layer has almost no direct negative-path tests;
- the adjudication's capital/cash-state requirement was not implemented;
- terminal PnL is an accrual, not observed cash;
- FIFO paired profit is a convention, not an identified lot allocation;
- quote policy, queue, cash finality, scalability and OOS persistence remain
  unknown;
- another Basket99 variant tests the wrong buy-side mechanism;
- paper implementation and all live paths remain NO-GO.

### Useful disagreement

Opus recommends the ten sibling 31×750 wallets as the next cheap falsification.
It also identifies three additional corrections: use authoritative on-chain
resolution rather than Gamma float thresholds, extend the one-hour lifecycle
tail to at least 24 hours, and bind dirty-tree/dependency provenance.

Qwen recommends first completing the missing capital/cash-state half of the
same frozen ledger: peak draw, simultaneous principals, capital-seconds,
per-window no-merge tails and T+1 non-recyclable rebate timing. It correctly
argues that no policy replay is eligible without those fields and a frozen raw
book tape.

## Adjudication and exact next gate

Both are right about different failure modes. Qwen's completion is mandatory
evidence repair, but it cannot answer whether `0x1dd2…` is merely the selected
maximum of a zero-edge cluster. Opus's sibling screen attacks that selection
risk cheaply, but running it through the incomplete v1 ledger would reproduce
the same provenance and capital holes.

Therefore:

### Prerequisite correction — artifact v2, not an experiment

Make one compact v2 accounting pass that:

1. hashes all behavior-relevant imports and refuses a dirty source tree;
2. verifies winners from authoritative payout data, not Gamma floats;
3. extends the post-window lifecycle bound to at least 24 hours and reports the
   exact query/watermark boundary;
4. computes peak confirmed draw, simultaneous principals, capital-seconds,
   per-window no-merge tails and rebate availability without backdating;
5. labels the current floor precisely: complete pairs are contractually
   mergeable, while unmatched residuals receive zero;
6. adds only the meaningful negative tests: buy, non-zero fee, wrong merge,
   duplicate event, merge-before-last-fill, missing/ambiguous source, and exact
   accounting reconciliation.

### One next experiment — frozen sibling-cluster falsification

Run the unchanged v2 logic over the ten other standard-adapter 31×750 addresses
already fixed in the receipt artifact. No re-ranking, parameter search, wallet
replacement or rebate extrapolation.

Pre-register these interpretation gates:

- primary metric excludes rebates;
- the statistical unit is the market window, aggregating wallets that share the
  same market shock—not fills or wallet rows;
- at least 7 of 10 sibling wallets must have positive pre-rebate contractual
  terminal PnL;
- pooled market-window PnL must be positive with t ≥ 2 under the predeclared
  aggregation;
- capital-normalized results, no-merge tails, allocation bounds and
  residual-zero-payoff cases must all be published;
- if pooled mean is non-positive, or `0x1dd2…` is an isolated positive survivor,
  retire mint-to-make;
- a pass authorizes one prospective causal paper design only. It does not prove
  OOS profitability and never authorizes live orders.

### If and only if the falsification survives

Freeze one mint-to-make policy independently of winner fills: complete-set
inventory, simultaneous maker asks, explicit size/timing/cancel/reprice rules,
exact residual and merge policy, and a hard capital cap. Replay it against a new
zero-loss raw book/trade tape with snapshot bootstrap, measured maker action
delay, queue ahead, late fills, reconnect invalidation and rebate excluded from
the primary result. Run one arm with a predeclared horizon and futility rule.

Do not use Basket99 as a proxy. Basket99 buys pairs and accumulates unmatched
first legs; the observed mechanism splits complete sets and sells inventory.
No reentry switch can turn one into the other.

## Signal and cross-venue work

No current result supports a signal threshold. Public Polymarket prices already
identified the eventual favorite in 31/31 Fresh31 windows at final-30 seconds,
and the corrected FIFO residual was only +$2.90. Gen75 should continue as a
passive, gap-accounted RTDS/Binance/Deribit capture. Its future use is one
pre-registered lead/lag and toxicity event study against the causal Polymarket
book, with the venue's taker delay applied. It must not be wired into the mint
arm or used to tune a threshold on the same sample.

## Operational state

- Gen78 paper runner: stopped and archived cleanly;
- Gen75 cross-venue capture: still running passively;
- executor/mintbot/lockbot: not running;
- mint place mode: hard-disabled;
- live orders and money movement: **NO-GO**.

The honest project status is not “close to the winners.” We now understand one
winner's inventory lifecycle substantially better and have repaired the feed
needed to test policies causally. We still do not know their quote policy, queue
advantage, capital return or whether their apparent edge survives across the
identical sibling cluster. Building a bot before answering those questions
would be faster execution of an unproven idea, not faster learning.
