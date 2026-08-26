# Independent review — Qwen seat (qwen3.8-max) — 2026-08-26

Read-only code inspection, no shell. Did not read the Codex review (independence).
Same packet as reports/2026-08-26-codex-sol-independent-review.md.

# Independent adversarial review — Polymarket 5m crypto system

**Method note:** I had read-only access and no shell, so this is code inspection, not execution. I could not run the test suite, recompute hashes, or open any SQLite ledger. I verified structural claims against source; every claim I couldn't verify is marked as such. Note also: the packet is pinned at `f53d12f`, but HEAD is `5d406df` — five commits later, including "Rebuild the live intent path" and "Give the mintbot its own brake". Gen89 was observed on code that was uncommitted even relative to the packet's own revision. I did not read the other reviewer's report in `reports/` (independence).

## Verification of packet claims

| Claim | Verdict |
|---|---|
| Board = basket99/97/95/100, hash `da5c9690f1475886` | ✓ Matches `strategy_board.py`; hash pinned byte-for-byte in `tests/test_strategy_board.py` |
| basket99 only arm with `patient_bids=False` | ✓ (also `improve_ticks=1`, ceiling 0.99, `new_pair_start_s=30`) |
| Momentum retired; mom10 gross +$5.18 vs fees $26.93 | ✓ Encoded in board comment and test assertion; numbers not independently reproducible (no Gen8x DB in repo) |
| `PREMIUM_FLOORS = ((120,1.001),(180,1.008),(240,1.025),(300,1.055))` | ✓ Exact match in `live/mintbot.py` |
| live_gate fail-closed, 3 conditions, 11 tests | ✓ `paper/live_gate.py` + exactly 11 tests in `test_live_gate.py` |
| "205 tests currently pass" | **Unverifiable here.** Exactly 205 `def test_` exist (count matches). `.pytest_cache/lastfailed` shows 2 failures, but for test names that no longer exist — stale cache, not evidence of current failure. I could not run them. |
| Queue-aware fills vs real prints, 65 ms latency, 400 ms lag bound | ✓ Structurally true (`PairWindow.on_trade` consumes queue-ahead; `action_latency_s=0.065`; lag bound in README/env) |
| **Gen88/89 results themselves** | **NOT in this repo.** No Gen77+ DB, capture, or run.log exists locally (`out/` archives stop at gen76-*). The ledger presumably lives only on the Ireland box. Every number in §3 of the packet is an assertion, not reviewable evidence. |

Two code facts the packet does not mention, both relevant below: (1) `tools/tape_backtest.py` already proved FINDING 2's late premium is **not capturable** by a balanced seller (−$1.60 to −$2.81/window, queue-optimistic); (2) the README's own lifecycle audit found holdout PnL AUC of **0.471/0.482** for the winner cohort — no economic edge. The packet cites the optimistic halves of both studies and omits the refuting halves.

---

## Concerns ranked by dollar risk

1. **You are benchmarking strategies against a market that may no longer exist.** The entire evidence base (FINDINGS 1–7, the 38 Gen88 windows) is Aug 18–25, inside the $1M August liquidity-reward program. Your own README documents that on Aug 26 the 5m products went to ~0 trades/window while books stayed quoted — the shape of a subsidy with the organic flow gone. Any capital decision made on Gen88 economics assumes a regime transfer for which there is zero evidence. This dominates every other risk: all other dollar amounts in this review are small compared to sizing capital off a dead regime.
2. **The headline Gen88 number cannot be reconstructed from anything I can see.** No ledger/capture/log for Gen77–89 in the repo. A review that cannot touch the data is trust, and "+$6.53 / −$20.57" is a $27 swing on a coin flip — see Q1.
3. **The paper engine omits the documented 250 ms `itode` taker delay** (README: "the current BTC market reports itode=true… 65 ms must never be used as taker latency"). Grep for `itode`/`250` across `paper/` returns nothing. The momentum arms were taker strategies simulated with a 65 ms *maker* proxy — and `MomentumWindow._execute` sweeps with **zero latency at all** (instant execution inside `on_books`). Every future taker arm will flatter itself in paper by ≥250 ms of market movement. This is the same failure class that just cost the mom arms their edge.
4. **Residual naked inventory is a design flaw, not bad luck.** `new_pair_cutoff_s=300` lets the engine open a new pair at T+295 that can never complete, carrying a naked 5-share leg into settlement every time. That single parameter manufactures most of the headline-vs-floor gap. There is no pre-settlement flatten anywhere in the accumulate path.
5. **Stale-intent ingestion hazard on the live bridge.** `live/executor.py` starts at `read_pos = 0` and replays the entire existing `paper/intents.jsonl` on every startup. That file currently holds **1,414 stale crossvenue-era intents** (`deribit_only`/`binance_only`, dead tokens, different record shape with `side_up`/`caps`). Today `enabled=[]` so nothing trades; but `type=="book"` records are *not* strategy-filtered and feed the G16 recycler/G17 lock-taker. Before anything is ever enabled, that file should be rotated away.
6. **Float-comparison bug at exact-ceiling books.** `_desired` tests `sum(improved_bids.values()) > start_cap` on floats. With best bids 0.32/0.67 (sum 0.990), IEEE sum is 0.99000000000000005 > 0.99 → basket99 withdraws even though it fits its ceiling *exactly*. This silently converts "market too tight" into "float noise", and it matters because the Gen89 diagnosis rests on exactly this boundary (Q7).
7. **Geoblock is blocked=true on both machines.** This is currently a non-risk only because the guards hold; I verified mintbot place mode and executor place mode both hard-`SystemExit`. Do not let any future "the numbers finally look good" moment erode that.

---

## Q1 — basket99: +$6.53 vs −$20.57

The packet misdescribes `floor_pnl`. It is not "liquidation at a conservative price". From `report.snapshot_one`: `worst_pnl = Σ(cash + min(residual, resid_shares − residual)) + invalid_floor` — i.e., mark the naked residual at **$0 (adverse settlement)**, matched pairs at $1. And the headline `pnl = cash + residual` marks the residual at the **actual realized outcome**. So:

- headline +6.53 = real settlement cash, **including the coin-flip on ~2 naked shares/window** (~76 shares over 38 windows → ±$38 swing, expected +$38);
- floor −20.57 = same cash with every naked share assumed to lose;
- the unbiased mark is the report's own `neutral_pnl` (50¢ each). Arithmetic: neutral ≈ floor + 76/2 ≈ **+$17** — *above* the headline. Your residual landed on the winning side only ~27/76 times (~36%), ~2σ below coin-fair. That's not optimism vs pessimism; it suggests the residual is **adversely selected**: the leg that fills but never completes is the leg takers were exiting. If that's real, the honest mark on residual is *below* 50¢, closer to floor than to headline.

**Which is honest?** Neither alone. The headline is honest-but-lucky/unlucky; the floor is honest-worst-case. The decision number is the FIFO `pair_edge` (already reported, excludes unmatched legs) plus an empirically estimated mark for the residual. **What settles it:** (a) implement a pre-settlement taker-flatten of the naked residual at T+285 with real fees, and judge realized cash — this converts the coin flip into a measured cost; (b) enough windows to estimate mean(outcome_pnl) — at ~$1/window outcome swing you need hundreds, not 38. Until then, "+12.7% ROC" and "−40% ROC" are both equally defensible readings of the same 38 windows.

## Q2 — FINDING 6 vs the basket board

It is a genuine contradiction as stated, and the "selectivity on the price axis" framing in `strategy_board.py` is a rationalization. Three points:

- The basket board is continuous/two-sided/maker on all four axes that anti-predict margin (both-sided −0.464, volume −0.363, fills/market −0.361, maker −0.230). Gen88 then returned a monotonic result in the *wrong direction for the selectivity hypothesis*: 0.99 (+12.7%) > 0.97 (+0.3%) > 0.95 (−11.3%). The least selective arm won. Your own 38 windows refute the gradient you built three arms to test.
- The honest escape hatch — margin ≠ dollars — is real: the biggest dollar winner in your study period (`0xb27…`, +$18.8k, 99.5% maker, always-on, per your README) has exactly the anti-predicted profile. FINDING 6 predicts a *ratio*, and small-denominator noise mechanically inflates it (pnl/volume at low volume; note volume itself correlates −0.363). Most of FINDING 6 is likely a denominator artifact, with maker-share the weakest (z −2.02).
- But the escape hatch doesn't rescue the board either, because the dollar-winner's regime was the subsidy regime, and per Q6's regime evidence that flow is gone. You are not building the opposite of what wins; you are building a careful machine for a game that may have ended.

## Q3 — momentum: where is the error?

In the tape backtest's fill assumptions. Not the fee model, and "signal not tradeable" is the conclusion, not the diagnosis. Verified in `tools/momentum_probe.py`:

- **Look-ahead entry pricing.** `tradeable()` fires when bucket-b mid − bucket-(b−1) mid ≥ 0.10, then prices the entry at `p1[1]` — the ask-VWAP of *the same bucket in which the move happened*. You only know the move at the bucket's end; the VWAP includes pre-move quotes. The probe bought the past. The gap between that VWAP and the post-move ask *is* the continuation being measured — booked twice.
- Exit at a future bucket's bid-VWAP: no depth, no impact, secondary but same direction.
- **Fees exonerated:** live mom10 paid $26.93 over ~65 round trips ≈ 3.4–3.5¢/share round trip at p≈0.5–0.6 — exactly `0.07·p·(1−p)` both legs. The fee model was right; gross was ~4× worse than tape (+$5.18 vs ~$21 implied). The fill model carried the whole error, as the board comment itself admits.
- Additionally the paper momentum arm executes with zero latency and no `itode` 250 ms taker delay (see risk #3) — further optimism the tape backtest didn't even have.

The autocorrelation (z +28.7) is probably real; it is not monetizable by a taker at your latency. Retirement was correct.

## Q4 — FINDING 5 vs FINDING 3

They are consistent in principle and inconsistent in the strength claimed. Calibration kills *directional* prediction; it does not kill pair-acquisition skill (buying matched pairs below $1), fee management, or timing. So "nobody predicts outcome, yet margin persists" can coexist. But the statistic as computed has four artifacts the packet doesn't carry:

1. **Survivorship in the denominator.** The sample is 78 wallets with >$1k volume in *both* periods — conditioning on period-B activity. A period-A star that blew up and quit is excluded, so "0/20 losers" is true *conditional on surviving*. 
2. **Overlapping periods.** Aug18–22 vs Aug22–25 share Aug 22 — mechanically correlated by construction.
3. **It may just be pair-cost style.** Your own README: discovery pair-cost persists at Spearman 0.826, while holdout actual-PnL AUC is **0.471/0.482** — i.e., style persists, profit does not. FINDING 5 likely re-discovers the same style persistence and relabels it "skill identifiable ex ante".
4. **Regime beta.** Every tertile had zero losers; the whole week was favorable. Persistence within one easy regime is not evidence of skill that survives the regime (see risk #1).

Consistent reading: persistent *mechanical* skill (cheap pair acquisition), not predictive skill, measured with selection bias, in a regime that may be over. The packet's "copying winners is not survivorship bias" overclaims; the design rules out *rank* survivorship but not *activity* survivorship.

## Q5 — 0xce50c96b: most likely mechanism, and arguing against your story

Your story — "intra-window spread capture in thin books" — is the wrong label, because the numbers don't fit spread capture:

- Books in these products run 1–2¢ spreads; the wallet's round trips are ~10¢ (0.477 → 0.580). You cannot attribute ten cents to a one-cent spread. That's **price-move capture**, not spread capture.
- 62% of fills are **taker**, paying ~3.5¢ round-trip fees. Passive scalpers don't cross; crossing that much means urgency — either informed taking against stale quotes or aggressive inventory management.
- Buying near 0.477 and selling near 0.580 means buying dips and selling rips: intermediating other people's urgency (retail chasing spot moves), or being the fastest reactor to spot repricing.
- The 00–08 UTC concentration is exactly when Polymarket books are thinnest and **stalest relative to spot**. FINDING 4 ("Binance doesn't lead the TWAP") is measured at the settlement horizon; it says nothing about seconds-horizon lead-lag inside the window, which is the only horizon that matters for a 10¢ scalp. Your own FINDING 4 doesn't refute this mechanism; it's the wrong instrument.
- 53.8% terminal accuracy is what you'd *require* of this mechanism — it exits before settlement — so it neither helps nor hurts the story; it kills any outcome-prediction reading.

Most likely actual mechanism: **the deepest two-sided quote provider in the thin overnight book, capturing retail urgency at 5–10¢ dislocations, plus informed taking when spot jumps — flow intermediation with an information overlay**, possibly shaped by the reward program's quoting incentives (which don't show in tape PnL but do shape behavior). Arguments against even this reading: (a) it's a selected maximum of 300 wallets — hour-concentration is partly a selection artifact (99 windows vs 8/11/16 elsewhere is also a sample-size confound); (b) replicating it requires speed you provably lack (250 ms taker delay) and overnight flow that the Aug-26 collapse suggests is gone. Your README already says it: "replicating it needs a signal we do not have."

## Q6 — sample size and stopping rule

38 windows ≈ 3.2 hours of one regime, scored on the luckiest of three marks. Decomposing Gen88: outcome swing ±~$38 on ~76 naked shares, per-window σ plausibly $1.5–2.5. To resolve the observed +$0.17/window at one-sided α=2.5%, 80% power: n ≈ (1.96+0.84)²·σ²/μ² → **~270 windows at σ=$1, ~1,080 at σ=$2**, before inflating for hour/regime clustering (windows are not iid; cluster-bootstrap by UTC hour). Your own README states the correct rule — N must come from a genuinely prior clean pre-period — and that no defensible N currently exists. Fine. But then +12.7% is noise by your own standard, and so is −11.3%.

Pre-registered design I'd accept:

- **Primary endpoint fixed in advance: floor-mark PnL (or post-flatten realized cash), not settlement headline, not ROC.** One arm (basket99) primary; the others exploratory, no picking the winner after looking — note that basket100 was already added *because of* the Gen88 trend, so the next cohort is data-snooped before it starts.
- Fixed horizon: **600 valid windows or 14 calendar days**, whichever first. No early stop for benefit; one interim futility look at 300 (halt if floor-mark mean ≤ 0).
- Validity gates that pause the clock rather than score: window validity ≥85%, median per-window volume ≥ P25 of the Aug18–25 baseline (regime check), event-lag within spec. Today this gate fails before the first window — the market isn't trading.
- Pass criterion: lower 97.5% CI bound on mean floor-mark PnL/window > 0 **and** above the fill-model-error hurdle (paper fills are model fills until calibrated — your words, not mine).

## Q7 — what you didn't ask

- **The packet is stale relative to its own subject.** It reviews `f53d12f`+uncommitted Gen89; HEAD is five commits past that, with the live-intent path rebuilt and mintbot given its own brake. The Gen89 "active_orders=0, market structure" diagnosis was made on code nobody can audit, and its arithmetic doesn't hold under any tick assumption: with the stated 1¢-spread books (0.32/0.33, 0.67/0.68), `_maker_price` caps improvement at `best_ask − tick`, so the improved pair sum is 0.99 (tick 0.01, modulo the float bug in risk #6) or 0.992 (tick 0.001) — never the "1.010" the packet claims; and **basket100 (ceiling 1.00) should still be quoting in either case**, with basket97/95 resting patient scaled bids below the book. "All arms active_orders=0" is inconsistent with the code as written against the market as described. Either the books were one-sided, windows were being invalidated by feed lag, or the uncommitted Gen89 code differed. "Diagnosed as market structure" is currently an article of faith.
- **Finding misstatements matter because they flatter.** `floor_pnl` misdescribed (Q1); FINDING 1 says "#58 at 0.33% margin" where your README says "0.33% of volume"; FINDING 2 sold as mintbot's rationale while your own `tape_backtest.py` proved the premium uncapturable by a balanced seller; FINDING 5 sold as ex-ante skill while your own lifecycle audit reports AUC ≈0.48. Individually small; as a pattern it's selection bias in the packet itself.
- **No live strategy candidate exists.** Momentum retired on live evidence; mint thesis demoted (#58/300); mintbot selling a premium your own tools say can't be captured; basket board unresolved between +12.7% and −40%; winner replication needs a signal you don't have; and the market itself stopped trading. The README's first line — **NO-GO** — is still the only defensible status. The greatest dollar risk in this repo is not any single bug; it's that a +$6.53 on 38 windows in a dead, subsidized regime becomes the reason someone turns a key.

**Bottom line:** the engineering discipline in this repo (fail-closed gates, invalid-window accounting, queue-aware fills, brake files) is genuinely good and I verified it. The economics are not established: the headline is luck-marked, the evidence is unreachable, the market may be gone, and the packet systematically presents the favorable half of its own findings. Keep it NO-GO until flow provably returns, the Gen88 ledger is produced and re-derived, and a pre-registered 600-window cohort on the floor mark clears zero.
