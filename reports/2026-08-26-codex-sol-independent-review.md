# Independent review — Codex (sol) seat, xhigh effort — 2026-08-26

Seat: `codex exec` v0.147.0, sandbox read-only, model_reasoning_effort=xhigh.
Packet: scratchpad/review_packet.md. Reviewer had read access to the repo and
verified claims against code rather than accepting the packet.
Qwen seat: launched separately, no output as of this writing.

## Ranked by dollar risk
1. **Unbounded if scaled: the “persistent winner skill” result selects on future performance.** It is not valid out-of-sample evidence.
2. **About 40% downside versus the claimed Gen88 bankroll:** `-$20.57` floor against an implied ~$51.4 bankroll. The strategy deliberately leaves unmatched inventory.
3. **Up to configured live caps if re-enabled:** withdrawn paper quotes can remain live for 240 seconds, while the paper model assumes 65 ms actions.
4. **$250/day conditional mint exposure:** the mintbot’s premium schedule is based on non-simultaneous, unnormalized tape VWAPs, even though the repo’s own tape backtest says late balanced selling loses.
5. **Unquantifiable governance risk:** no Gen88/89 database, capture, or run log exists locally, so the headline results cannot be independently reproduced.
## Q1 — `+$6.53` versus `-$20.57`
The packet describes `floor_pnl` incorrectly. It is **not liquidation at a conservative market price**.
Code computes:
- `pnl = cash + payoff of the actual winning inventory`
- `neutral = cash + 0.5 × all ending shares`
- `floor = cash + min(Up inventory, Down inventory)`, meaning every unmatched residual loses
See [pair_engine.py:551](/C:/Users/nteni/project-fail/paper/pair_engine.py:551) and [report.py:135](/C:/Users/nteni/project-fail/paper/report.py:135). The report explicitly labels floor as adverse-outcome settlement PnL, not liquidation proceeds, at [report.py:405](/C:/Users/nteni/project-fail/paper/report.py:405).
Therefore:
- `+$6.53` is honest **historical contractual terminal PnL** if the official outcomes are correct and positions are eventually redeemed.
- `-$20.57` is honest **stress loss**.
- Neither is the honest forward edge. That is `neutral$`, `edge$`, or—better—actual residual liquidation proceeds after bid depth, latency, fees, and slippage.
The $27.10 gap shows how outcome-sensitive the tiny profit is. The packet omits the exact columns needed to judge it: `edge`, `neutral`, `outcome`, and unmatched shares.
What settles it:
1. Produce the missing Gen88 ledger/capture.
2. Report per-window cash, paired edge, Up/Down residual, neutral PnL, floor, and actual outcome.
3. Pre-register a forced residual liquidation time.
4. Reconcile actual pUSD, positions, merge/redemption cash, gas, and taker fees.
Until then, do not headline `+12.7% ROC`. The ROC denominator also assumes capital is released after 15 minutes ([report.py:98](/C:/Users/nteni/project-fail/paper/report.py:98)), while the reviewed live executor has no redemption path and direct split/merge is disabled.
## Q2 — Are the baskets the opposite of the winners?
Yes. The current justification is rationalization.
`basket97/95` are called a “selectivity test,” but they remain continuous, two-sided, maker strategies. `patient_bids=True` merely rests both bids below the book in every eligible window ([strategy_board.py:53](/C:/Users/nteni/project-fail/paper/strategy_board.py:53), [pair_engine.py:121](/C:/Users/nteni/project-fail/paper/pair_engine.py:121)). That is price selectivity, not the winners’ time/market/side selectivity.
There is one logically consistent reading: cross-wallet correlations do not disprove an independent pair arbitrage. But then the winner analysis supplies **zero support** for basket99. Basket99 must stand solely on its own paired, liquidation-adjusted economics.
Worse, the directly relevant tape test says symmetric makers fail:
- Pre-open: `-$4.21` to `-$11.03/window`
- Book-anchored: roughly `-$0.47` to `$0`
- Late balanced selling: `-$1.60` to `-$2.81/window`
That conclusion is embedded in [tape_backtest.py:16](/C:/Users/nteni/project-fail/tools/tape_backtest.py:16). It is more relevant than ecological correlations across wallets.
Finding 6 is also contaminated by:
- Margin’s denominator: low volume mechanically raises `PnL/volume`.
- Omitted maker rebates/rewards, while taker fees are deducted.
- Confounding: skilled traders may be selective because they possess a signal; selectivity is not necessarily the signal.
## Q3 — Momentum error
The fee model is not the problem. The backtest is.
The `+1.96¢/share` result is not causal executable evidence:
- It computes the signal using the entire current 10-second bucket, then “enters” at that same bucket’s full ask VWAP.
- It exits at the full VWAP of a later bucket.
- It assumes availability at VWAP without displayed depth.
- It permits overlapping signal observations.
- It uses raw `trade_history`, bypassing the repo’s V2 normalization.
See [momentum_probe.py:69](/C:/Users/nteni/project-fail/tools/momentum_probe.py:69) and [momentum_probe.py:115](/C:/Users/nteni/project-fail/tools/momentum_probe.py:115).
The paper engine is still optimistic: momentum sweeps the current displayed book immediately after recognizing the signal; it does not apply `action_latency_s` to entry or exit ([momentum_engine.py:123](/C:/Users/nteni/project-fail/paper/momentum_engine.py:123)).
`+$5.18 gross - $26.93 fees = -$21.75` is decisive for this implementation. The signal may describe trade clustering or order splitting, but it is **not tradeable as tested**. Retiring it was correct.
## Q4 — Calibration versus persistent skill
They are theoretically compatible. A calibrated terminal probability can coexist with:
- spread capture,
- latency or order-flow forecasting,
- intra-window path timing,
- inventory management,
- rebates,
- liquidity provision.
Also, calibration only establishes something like `E[outcome | price] ≈ price`. It does not establish `E[outcome | price, feature] = price`. “Perfect calibration” does **not** prove nobody has directional information.
Empirically, however, Finding 5 is broken:
- `top_setters.py` sorts wallets by PnL and truncates to the requested limit ([top_setters.py:110](/C:/Users/nteni/project-fail/tools/top_setters.py:110), [wallet_metrics.py:172](/C:/Users/nteni/project-fail/tools/wallet_metrics.py:172)).
- `winner_persistence.py` then intersects the two top-300 files before testing persistence ([winner_persistence.py:65](/C:/Users/nteni/project-fail/tools/winner_persistence.py:65)).
- Its alleged “period-A top-20” is chosen only from wallets that already survived into period B’s top 300 ([winner_persistence.py:113](/C:/Users/nteni/project-fail/tools/winner_persistence.py:113)).
From the saved inputs:
- Period A eligible wallets: 222
- Common future survivors: 78
- Only **9 of period A’s actual top-20 by margin** appear in period B
- The other 11 are silently replaced
- Period A has 1,008 resolved asset-windows; period B has 1,984, with materially different asset coverage ([lb_h1.json:3038](/C:/Users/nteni/project-fail/out/lb_h1.json:3038), [lb_h2.json:2062](/C:/Users/nteni/project-fail/out/lb_h2.json:2062))
- The periods overlap one inclusive boundary window
So `rho +0.517`, `4.03%`, and `0/20 losers` are future-conditioned. They do not prove persistence.
Finding 1 is also mislabeled: `0x1dd2...` is #58 because the file is PnL-sorted. Its margin rank among those same 300 is approximately **#267**, not #58. Its 0.327% margin itself is reproduced in [lb7d.json:6120](/C:/Users/nteni/project-fail/out/lb7d.json:6120).
## Q5 — What is `0xce50...` actually doing?
Most likely: **short-horizon price-path timing with mixed passive/aggressive execution and large inventory**, not vanilla spread capture.
I reran the normalized BTC cycle analysis for Aug 18–25:
- 134 traded markets
- **0 complete-set round-trip markets**
- 51 same-token cycle markets
- 41,219 round-tripped shares
- `+$5,314` net cycle edge
- `+12.89¢/share`
- 48-second median holding time
- Only 48.2% of cycle shares had both legs as maker
- 460,402 shares remained open in fill-only reconstruction
The cycle logic is at [wallet_cycles.py:196](/C:/Users/nteni/project-fail/tools/wallet_cycles.py:196).
That argues against simple spread capture:
- A 12.9-cent move over 48 seconds is far larger than a one-cent spread.
- A majority of cycles use a taker on at least one leg.
- Terminal 53.8% accuracy is irrelevant if positions are flattened before settlement.
- The enormous open inventory means public fills alone do not identify transfers, minting, or total capital.
The 92% overnight claim is not independently reproducible from a committed analysis script. Plausible alternatives include weaker overnight price discovery, reacting to informed flow, momentum, temporary dislocations, or omitted rebate/reward economics. One wallet over one week is not proof of a stable hour effect.
## Q6 — Minimum sample and stopping rule
Thirty-eight windows is noise.
A 58% win rate is approximately 22/38:
- One-sided binomial p-value versus 50%: **0.209**
- 95% Wilson interval: **42.2%–72.1%**
Roughly **304 independent windows** are required for 80% power to distinguish 58% from 50% at one-sided α=2.5%. That still tests only win rate, not economic PnL.
For PnL, the minimum cannot be calculated from aggregates. With per-window standard deviation `σ` and minimum acceptable mean edge `δ`:
`n ≈ 7.85 × (σ/δ)²`
My pre-registered rule would be:
1. Freeze basket99 alone, its board/model hashes, assets, hours, sizing, and liquidation policy.
2. Primary endpoint: net cash after forced residual liquidation, all fees, gas, and slippage—not terminal headline PnL.
3. Count one five-minute timestamp across correlated assets as one cluster.
4. Use `N = max(400, ceil(7.85 × (σ/δ)²))`, with σ fixed from the existing pilot.
5. Require at least 100 filled clusters and at least 14 consecutive calendar days.
6. One final look only; one-sided 97.5% lower confidence bound must exceed both zero and the chosen economic hurdle.
7. If data quality or fill count misses the pre-registered requirement, call it inconclusive. Do not extend until profitable.
Given 89 generations and repeated arm changes, existing data must be treated as development data, not confirmatory evidence.
## Q7 — The real problems
- **This is not currently a live trading system.** Executor `place` is hard-disabled at [executor.py:285](/C:/Users/nteni/project-fail/live/executor.py:285); mintbot `place` is hard-disabled at [mintbot.py:128](/C:/Users/nteni/project-fail/live/mintbot.py:128); direct chain split/merge is disabled at [chain.py:200](/C:/Users/nteni/project-fail/live/chain.py:200); live enablement is empty.
- **Finding 2 is overstated.** `pair_cost_curve.py` adds separately volume-weighted Up and Down trades inside 30-second buckets. Those are not simultaneous executable bids. It also queries raw V2 rows ([pair_cost_curve.py:56](/C:/Users/nteni/project-fail/tools/pair_cost_curve.py:56)), contrary to the packet’s normalization claim. A rising aggregate pair VWAP does not mean a balanced minted set can be sold at that price.
- **The dormant bridge has a dangerous cancellation hole.** When both paper orders disappear, `live_quotes()` emits nothing ([cohort_engine.py:132](/C:/Users/nteni/project-fail/paper/cohort_engine.py:132)). The executor therefore retains its last desired quote until the 240-second stale timeout ([executor.py:556](/C:/Users/nteni/project-fail/live/executor.py:556)). Gen89’s “active_orders=0” scenario is exactly the state that would expose this.
- **Latency parity is fictional.** Paper models 65 ms, but intents emit once per second and the executor loops once per second ([run.py:393](/C:/Users/nteni/project-fail/paper/run.py:393), [executor.py:634](/C:/Users/nteni/project-fail/live/executor.py:634)). After any live fill, paper inventory and real inventory would diverge; authenticated reconciliation is explicitly unfinished.
- **The review packet is not revision-accurate.** The clean checkout is `5d406df`, twenty commits after `f53d12f` and two ahead of `origin/main`. Gen88/89 artifacts are absent, so the stated results are assertions, not reviewable evidence.
- **The test-count claim is only partly verified.** I collected exactly 205 tests. I could not independently run them because this read-only environment provides no writable pytest temporary directory.
Blunt conclusion: basket99 may contain a real cheap-pair effect, but this packet has not demonstrated it. The winner evidence is future-selected, the momentum backtest is non-causal, the pair-premium curve is not executable pair pricing, the 38-window headline mixes pair economics with residual outcome risk, and no reviewed live path can trade. Removing the live interlocks now would be gambling, not deployment.
