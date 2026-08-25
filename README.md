# project-fail — Polymarket 5-minute crypto trading system

Research → paper A/B → guarded live execution for Polymarket's 5-minute
BTC/ETH/SOL/XRP "up or down" markets. One question end-to-end: *is there a
replicable, low-capital edge, and what execution captures it?*

**Status: NO-GO for real money.** Corrected winner forensics shows several
different behaviours: maker accumulators, two-sided churn, mint/transfer-
supplemented sellers, and pre-finality/cashout activity. Trading both tokens is
common but does not prove simultaneous quoting, hedged carry, or minting. The
mint strategy is therefore a hypothesis to test, not "the winners' mechanic."

---

## 1. The Ireland box (the only machine that may trade)

| | |
|---|---|
| Host | `ubuntu@3.254.130.64` (AWS eu-west-1 — Polymarket's recommended region) |
| SSH | `ssh -i ~/.ssh/pm_deploy ubuntu@3.254.130.64` |
| Code | `~/project-fail` (deployed by `scp` from the local repo — **not** a git checkout) |
| Python | `~/project-fail/.venv/bin/python` (always use the venv binary) |
| Measured latency | CLOB `/time` GET **28 ms** median; intent-to-1s-poll **566 ms** median / **857 ms** p90; new-order lower bound ~**594 ms** median (POST acknowledgement unmeasured) |

**The UK/local machine is paper-and-tooling only** (trading from the UK is
blocked). The geo interlock (`DEPLOY_REGION=eu-west-1` in the box `.env`)
hard-blocks every place-mode component anywhere else.

### tmux sessions on the box

| Session | Command it runs | What it is |
|---|---|---|
| `paper` | `python -m paper.run` | the 49-arm paper A/B (writes `paper/paper.db`) |
| `shadow` | `LIVE_EXECUTOR_MODE=shadow python -m live.executor` | executor soak: real client + feeds, orders suppressed |
| `mintbot` | `MINTBOT_MODE=shadow python -m live.mintbot` | feed/quote soak only; place mode is currently forbidden |

View: `tmux attach -t paper` (detach `Ctrl-b d`) or `tmux capture-pane -t paper -p | tail -20`.

### The brake

```bash
touch ~/project-fail/paper/KILL
```
Every component checks this file each loop: the executor cancels everything and
exits; the mintbot cancels all asks and exits (balanced pairs can merge, but any
unpaired residue remains outcome risk); the paper gate stops emitting intents.
Remove the file before restarting anything.

---

## 2. Getting status and reports

All read-only; run from anywhere with the SSH key.

**Full paper report (all 10 columns + races)** — the canonical status:
```bash
ssh -i ~/.ssh/pm_deploy ubuntu@3.254.130.64 'cd ~/project-fail && ./.venv/bin/python -m paper.report'
```

**Heartbeat** (is the runner alive; feeds; per-arm action counts):
```bash
ssh -i ~/.ssh/pm_deploy ubuntu@3.254.130.64 'grep "hb |" ~/project-fail/paper/run.log | tail -1'
```

**Mintbot per-window results** (minted / sold per side / merged / est PnL):
```bash
ssh -i ~/.ssh/pm_deploy ubuntu@3.254.130.64 'cd ~/project-fail && ./.venv/bin/python -c "
import sqlite3
for r in sqlite3.connect(\"live/mintbot.db\").execute(\"SELECT asset,slug,minted,sold_up,sold_dn,round(sold_usd,2),merged,mode FROM mint_windows ORDER BY ts DESC LIMIT 12\"): print(r)"'
```
Plus live narrative: `tail -30 ~/project-fail/live/mintbot.log` (`MINTED`, `ASK`,
`FILLED`, `CLOSE ... est_pnl`).

**Telegram**: the paper runner pushes the phone-formatted report every
`PAPER_TG_MINS` minutes (monospace, sorted by pnl desc) — configured via `.env`.

**Legacy benchmark vs real winners** (local machine — currently not a launch
gate because its outcome/fill comparison is not execution-normalised):
```bash
python winner_bench.py --hours 1
```
Prints the real-wallet pool/best/median for the same windows next to our arms.

**Corrected wallet forensics**: `python tools/top_setters.py --hours 24` uses
official resolved slugs, full token lifecycles, per-token inventory, and
read-only ClickHouse external tables. It separates direct CTF events from
unexplained inventory and reports matched-share buy/sell price-sum proxies.
`tools/latency_probe.py` measures GET/feed/poll surfaces while leaving order
POST acknowledgement explicitly unknown.

---

## 3. Legacy broad strategy roster (49 paper arms + accumulators)

Everything races simultaneously in `paper/run.py::STRATEGIES` on the same tape.
This is an exploration board, not 49 independent confirmations; multiple
comparisons and shared fill assumptions make a one-window leader meaningless.
Prefixes are composable lenses over the same signals:

| Prefix | Meaning |
|---|---|
| *(none)* | original research arm, f-skim fills (f=0.2 of each print) |
| `xf_` | exit-first twin: asks anchored at avg entry +2¢, forced taker dump last 40s |
| `ta_` | time-aware fair (`fair_up_t`: tanh, scaled by √(time left)) + late-floor |
| `lv_` | live-size/cap approximation; **not execution parity** because queue position, order acknowledgement, partial fills and exact live order state are absent |
| `sq_` | 1.0 s tail-stress reference for four execution-shaped candidates |
| `mint_` | mint-basis hypothesis: 20 sets at $1, never bids, asks track fair, matched leftovers merge; current paper fills do not prove profitability |

Signals: `twap` (Chainlink TWAP vs window ref), `binance`/`deribit` (external
spot), `mid` (market mid = "neutral"), `pair` (1 − other side's last price),
`pair_hl` (1 − other side's **best ask** — beat last-price anchoring by ~+84
over 52 windows), `twap_t` (time-aware). Confirm lists gate entries on feed
agreement. Board cadence (`PAPER_REQUOTE_S`) is a sensitivity; execution-shaped
twins use the measured 0.60 s lower-bound cadence and `sq_*` keeps a 1.0 s tail
stress case. Neither includes order POST acknowledgement.

Accumulators (not arms): `lock_arb`/`split_sell` (book-crossing set arb at 1.0 s
persistence = today's pipeline) and `lock_fast`/`split_fast` (0.15 s = in-process
pipeline). **Verdict from live probes: the taker-arb edge is ≈ zero** — the
lockbot went 0-for-lifetime on completed locks and was retired.

Key races the report prints: every `X vs xf_X / lv_X`, `sq_ vs lv_` (value of the
fast pipeline), `pair_mm vs hl_pair vs mint_hl` (entry mechanics), `mint_hl vs
mint_tw` (ask anchor).

### Fill-model limits
Fills execute at the posted quote and settlements skip missing references, but
the engine still has no queue model or order acknowledgement. `live_sim` can
absorb an entire crossing print while the gate emits only five ask shares, and
same-price refresh behaviour differs from the executor. Paper is useful for
rejection and comparison; it is not currently a production parity proof.

---

## 4. Live components

### `live/executor.py` — quote executor (file-driven)
Consumes `paper/intents.jsonl` from the paper runner's LiveGate. Modes
`log-only` / `shadow` / `place`. Guards G1–G17: geo interlock, startup/exit
cancel-all, per-action try/except, window-end cancels, $5 order cap,
holdings-gated sells, session exposure cap, **balance-based day stop persisted
per UTC day** (`live/day_baseline.json` — trips on any wallet drift it didn't
cause, incl. manual trading on the same wallet), action budget, KILL, rotation-
safe reader, close-dump, placement-time spend cap with cancel refunds, post-only
throttle (10 s cooldown), ledger tripwire, pair-recycler + lock-taker (dormant).
Enabled strategies: `paper/live.json`. The latest quote-intent probe measured
566 ms median / 857 ms p90 in the one-second poll loop before REST. Book-driven
arb intents also have a one-second producer throttle and can approach two
seconds.

### `live/mintbot.py` — experimental mintbot (in-process; shadow only)
Per window per asset: `splitPosition` mints $20 of sets in the first 60 s →
maker asks on both sides, anchored `1 − opposite_ask + 2¢`. It consumes
`price_change` deltas, sends the required heartbeat, verifies the old pair is
gone before one batch replacement, posts five-share clips, preserves queue
priority with a three-tick/five-second band (six-tick adverse override), and
stops quoting after asymmetric fills. A joint-sum
floor constrains a quoted pair but cannot guarantee paired fills. Position
polling still cannot reconstruct exact fill prices, so reported PnL is not yet
authoritative. Keep this component in shadow until the remaining gates and the
strategy edge are proven.

### `live/chain.py` — Polygon layer (no web3 dependency)
Raw JSON-RPC + eth_account. RPC pool w/ fallback (reads) but **single-attempt
broadcasts** with honest semantics: `PreflightError` = $0 moved (estimateGas
stage), `BroadcastUncertain` = may be in mempool, reconcile via balances.
Nonce-locked sends (4 assets share close boundaries). Minimal ABI encoder
(static types + one `uint256[]`, guarded). CLOB V2 contracts: CTF
`0x4D97...6045`, pUSD collateral `0xC011...82DFB`, and CTF Exchange
`0xE111...996B`. USDC.e is only an onramp input after the April 2026 migration.

### Minter wallet (EOA — separate from the site account)
`0xbb791E91F284E077a0a848C821690BB6A2dcfda7` — separate EOA, keys only in the
box `.env`. Any legacy USDC.e must be wrapped into pUSD before it can serve as
CLOB V2 collateral. V2 split/merge requires the collateral-adapter route; the
old direct-CTF scripts are disabled until that route and its approvals are
ported and verified. Mintbot place mode is also hard-disabled in code.

### `live/lockbot.py` — RETIRED
In-process taker set-arb. Lifetime: ~10 detections, 0 completed locks, ~$4.35
tuition. Lessons that now protect the mintbot: marketable-order amount rules
(2-decimal dollars, $1/leg minimum), fill→sellable indexing lag, adverse
selection on crossed books. Kept in-repo as reference.

---

## 5. Operating rules (standing, user-set)

1. **Parity rule**: nothing changes in production without paper/replay first.
   A green `lv_` arm alone is insufficient; order/fill/position reconciliation
   and measured execution behaviour must also pass.
2. **Every runner restart archives the DB**: verify the runner is DEAD first
   (`tmux kill-session -t paper`, then PID-checked wait), then
   `mv paper/paper.db paper/paper_genN_<date>end.db && rm -f paper/intents.jsonl`,
   then start fresh. Never mix generations in one sample.
3. **Launch boundary**: Claude prepares everything; **the user runs every
   place-mode launch and every command that moves money**. Keys go from
   MetaMask to the box `.env` by the user's hands only.
4. **Never `pkill` on the box.** The tmux server's argv contains session command
   strings — pattern kills murder every session (it happened three times).
   `tmux kill-session` + PID-targeted `kill` only.
5. Deploys: local edit → syntax check → `scp` → verified restart → commit+push
   to `main`. Reports always full 10-column output, sorted by pnl desc.

### Launch commands

No place-mode or approval command is published while the launch gate is closed.

### `.env` on the box (names only — never commit values)
`POLY_PRIVATE_KEY`, `POLY_FUNDER`, `POLY_SIGNATURE_TYPE` (site/CLOB identity) ·
`MINTER_PRIVATE_KEY`, `MINTER_ADDRESS` (mint EOA) · `DEPLOY_REGION=eu-west-1`
(geo interlock) · `TELEGRAM_*` (reports) · `PAPER_LIVE_INTENTS`, `PAPER_TG_MINS`,
`PAPER_SUMMARY_MINS`, `PAPER_REQUOTE_S` · optional `POLYGON_RPC_URL`,
`MINT_USD`, `MINT_DAY_CAP`, `MINT_SPREAD`, `LIVE_EXECUTOR_MODE`, `MINTBOT_MODE`.

---

## 6. Generations & DB archives

Every generation = one clean sample under one code state; archives live next to
the DB as `paper/paper_genN_<date>{start,end}.db`.

| Gen | When (UTC 08-24) | What changed |
|---|---|---|
| ≤5 | morning | research arms → fill model v2 → lv_ parity twins |
| 6 | 17:32 | full lv_ coverage (45 arms), hl_pair debut |
| 7 | 18:06 | **15 code-review fixes** (fills at quote, settlement honesty, executor guards) |
| 8 | 19:22 | lock/split 1 s persistence (kills flicker-arb fantasy) |
| 9 | 20:06 | whole board 0.15 s requote, sq_ slow twins, lock/split fast tiers |
| 10 | 21:53 | mint_hl / mint_tw debut + mintbot v1 shadow |
| 11 | 22:15 | **mintbot v2: 15 mint-stack review fixes**; paper mints 20 sets; order-sized sells restored; budget fix |

Audit verdict 2026-08-25: the neutral/pair/mint launch gates are **closed**.
The previous winner taxonomy, execution-parity claim, and latency attribution
were not supported strongly enough to justify real-money deployment.

---

## 7. History: the incident that shaped the guards

First live session (gen-3 era) lost ≈ $10 in 15 min because the executor
compared its **signer** address against fills that carry the **funder/proxy**
address → ledger blind → inventory cap never engaged → ~130 stale $5 bids
refilled on falling sides. Every guard since — authoritative balance/positions
data plane, placement-time caps, day-stop on wallet drift, KILL, parity rule,
paper-first — exists because of it. The trades feed multi-counts maker fills;
only balances and positions are truth.
