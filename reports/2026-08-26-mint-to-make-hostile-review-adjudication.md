# Mint-to-make hostile-review adjudication — 2026-08-26

## Bottom line

`0x1dd2…51c2` is the first address for which this project has defensible evidence
of the sequence **position-adapter split before the window → maker sells on both
outcomes → later merge**. It is not yet evidence of a reproducible quote policy
or cash-realized profitability.

The old economic narrative was materially wrong. A 50-cent mark on the residual
made ordinary low-price outcome selling look like a large directional signal.
On a chronological FIFO allocation, only $2.90 of the fresh $60.27
settlement-marked result is price-relative residual performance; $57.36 is
paired-sale surplus. The earlier frozen period shows the same broad pattern:
approximately $1,360.76 paired-sale surplus and $162.46 price-relative residual
performance before rebates. Those values are post-hoc diagnostics, not frozen
profitability evidence, and the lot allocation is not identified by public fills.

The one next experiment is therefore an integer-exact historical accounting
ledger, not a bot and not another parameter arm.

## Independent review protocol

- Immutable revision: `62178d93c4a9f8d660667423cba9b17e22a214ba`.
- Claude seat: exact `claude-opus-5`, effort `max`, safe mode, plan permission,
  read/grep/glob tools only, no fallback.
- Qwen seat: exact `qwen3.8-max`, safe mode, no fallback. The completed review
  ran with tool auto-approval inside a disposable detached worktree containing
  only the immutable revision and the five hashed artifacts. The worktree stayed
  clean and was removed after review; the primary worktree stayed clean.
- The reviewers received the same original packet independently. Neither saw the
  other's opinion.
- Two earlier Qwen launches are not review evidence: sandbox mode paused at
  Node `--inspect-brk`, and the first non-sandbox run could not approve read
  tools. Both were terminated without output or repository changes.

Primary evidence:

- receipt candidates:
  `073886fe970707d23927dfcc30342b31eb7fc2c6cef5717d49b1535c374be651`;
- receipt attribution:
  `4c37c45aaffeb925e72dd6aab1ef0985b8a9eeaa81f0d397d3fc1898d4363f31`;
- `0x1dd2…` rebate evidence:
  `c2a2d60e909ee4da0e21e13f2cb58c4880fd48715ea0910b277c5cf8734f753d`;
- `0x9d57…` rebate evidence:
  `586832f28481ed4abc9a52e8be52919817d4cc151595c5f42b3c0a0a215ca947`;
- earlier frozen seven-day aggregate:
  `ecd98ac5d03427e26f9324b0327c6b2f0f73539d4e62f50773cd1f8552ae0825`.

## Findings accepted

1. **The profit-bearing fresh numbers are not frozen evidence.** The receipt and
   rebate artifacts are reproducible, but no committed artifact contains the
   1,628 exact owner fills, paired/residual allocation, per-window economics, or
   capital cashflow. The current report overstates reproducibility.
2. **The merge equality needs an artifact.** A bounded integer-only rerun
   corroborated 1,628/1,628 V2 owner rows, zero buys, 31 splits totaling
   23,250,000,000 base units, and 24/24 merges after the last fill satisfying
   `merge = min(split - sold_up, split - sold_down)` exactly. That query and
   per-window ledger are not yet hash-bound. The result proves equality in the
   observed split-minus-CLOB ledger, not the wallet's complete balance; starting
   inventory and external ERC-1155 transfers are absent.
3. **The 50-cent decomposition is not signal evidence.** In
   `tools/lifecycle_cohort.py`, weighted residual hit and the reported
   `directional_pnl` are algebraically coupled. A seller of a 33-cent outcome is
   expected to be short the loser roughly 67% of the time. Scoring that against
   50% manufactures apparent prediction.
4. **Settlement-marked PnL is not cash-realized PnL.** Redemption, pUSD timing,
   transfers, Relayer/builder costs, and seven no-merge tails are unobserved.
5. **Capital efficiency is unresolved.** Adjacent 750-set principals overlap;
   proceeds become reusable only when authoritative balances confirm them.
   Volume and cumulative split principal are not return denominators.
6. **The rebate is an overlay.** The exact-condition endpoint join is sound, but
   it records a mutable/discretionary rebate amount, not payment finality or a
   scalable per-trade rate.
7. **The legacy mintbot is not repairable into this candidate.** Its EOA path,
   timing, sizing, position-poll fill inference, float/truncating merge behavior,
   and absent cross-window capital ledger do not model the observed mechanism.
8. **Public fills do not reveal a quote policy.** Submission, cancellation,
   unfilled quotes, queue priority, and continuous two-sided presence remain
   unidentified. Replaying the winner's fills as our fills would be circular.

## Corrected economics

For an excess sold amount `E`, unmatched sale cash `C`, and the retained
complement's payoff `Y`:

```text
residual_actual = C + E*Y - E
residual_50     = C - 0.5*E
direction_50    = E*(Y - 0.5)
```

Consequently `0.5 + direction_50/E` is the reported weighted hit rate by
identity, not a second observation. The price-implied break-even retained-outcome
rate is the share-weighted `1 - sold_price`.

| Slice | excess shares | 50c direction | price break-even | observed hit | FIFO price-relative residual | FIFO paired surplus |
|---|---:|---:|---:|---:|---:|---:|
| Fresh31 | 236.005418 | +$42.369876 | 66.7226% | 67.9529% | +$2.903666 | +$57.361797 |
| Frozen discovery | 10,291.205737 | +$1,845.988655 | 66.5590% | 67.9375% | +$141.866227 | +$836.960845 |
| Frozen later slice | 3,872.022548 | +$448.515146 | 61.0516% | 61.5835% | +$20.595605 | +$523.803736 |

The fresh total before rebate remains +$60.265 under official-outcome marking;
the interpretation changes. A proportional side-average allocation yields about
$27.19 paired and $33.08 residual, while admissible excess-fill allocations put
residual PnL between about -$43.01 and +$100.69. Total PnL is invariant, but the
story is not. FIFO is a useful frozen convention because the repo already uses
it elsewhere; it is not proof of the wallet's intended lot policy. The final
artifact must publish FIFO, proportional, and allocation bounds.

The earlier aggregate JSON alone cannot produce this table. A bounded raw
ClickHouse rerun covered 2,016 resolved BTC windows and 128,352 normalized owner
legs, all maker sells and zero buys. Because the new statistic was defined after
both old slices and Fresh31 were seen, all are discovery. The old later slice is
not a valid confirmatory holdout for this claim.

## Reviewer claims rejected or narrowed

1. **`clob_atomic=0` does not mean the classifier failed.** Candidate SQL first
   excluded 40,653 exact exchange-factory operations. Every emitted candidate
   had zero `trade_history` rows; a real excluded exchange receipt classified
   `clob_atomic` correctly. `0xADa100874…` should be renamed the V2 exchange
   outcome-token factory/adapter, not a deprecated legacy factory.
2. **The counterparty is not proven to be a Safe or Relayer-owned.** Receipt
   evidence proves the adapter counterparty address only. Beneficial ownership,
   bytecode type, and Relayer involvement remain open.
3. **The earlier negative 50-cent diagnostic is not evidence that paired-sale
   economics failed.** Corrected FIFO diagnostics are positive in both old
   slices, though still post-hoc and not receipt-attributed.
4. **Absent residual alpha would not kill mint-to-make.** It would kill a
   directional-signal claim. Paired spread plus defensible rebates could still
   support an inventory business if causal queue, cost, capital, and tail-risk
   tests pass.
5. **The 31×750 pattern is crowded, but Qwen overstated the count.** The exact
   receipt artifact contains 11 such addresses total: `0x1dd2…` plus ten outside
   the frozen cohort, not eleven outside it. Crowding and queue competition must
   enter later execution tests.
6. **Maker role is supported but not venue-authoritative metadata.** The two
   pinned exchange addresses match Polymarket's current authoritative contract
   registry and the V2 event structure. The frozen artifact should nevertheless
   bind contract-version provenance and fail closed on unknown exchange rows.

## Verdicts

| Question | Verdict |
|---|---|
| Address-level split/sell/merge mechanism | **GO as a forensic mechanism**, narrowed to the observed ledger |
| Historical cash profitability | **INCONCLUSIVE** |
| Exact accounting replay | **GO** |
| Counterfactual quote-policy replay | **BLOCKED** pending the accounting ledger and one frozen manifest |
| New paper arm | **NO-GO yet** |
| Live orders or money movement | **NO-GO** |
| Directional signal | **NO evidence**; retire the 50-cent hit narrative |

## One next experiment

Build one compact fail-closed `mint1dd2` accounting artifact over Fresh31 and the
already-frozen seven-day period. It must:

1. bind exact integer SQL, mappings, schemas, watermarks, contract versions,
   source revision, receipt artifacts, and daily condition-matched rebate rows;
2. emit per condition every split, owner fill, external ERC-1155 transfer, merge,
   redemption, and pUSD movement in `(block_number, log_index)` order;
3. conserve integer token and cash base units, distinguish observed-ledger from
   complete-wallet balance, and fail on ambiguous/non-V2 rows or post-merge fills;
4. report FIFO, proportional, and allocation-bound paired/residual economics;
5. report settlement-marked, executable-flatten, zero-payoff-floor, and cash-realized
   states separately;
6. calculate peak confirmed cash draw, simultaneous principals, capital-seconds,
   no-merge tails, and rebate timing.

Stop immediately on any conservation/provenance failure. Proceed to one causal
policy replay only if the historical mechanism persists and its spread economics
remain positive after documented non-discretionary costs under an externally
declared capital/risk hurdle. Do not infer the quote policy from winner fills,
do not tune a directional threshold, and do not add another strategy arm.
