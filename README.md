# project-fail — Polymarket 5-minute crypto trading system

Research → paper A/B → guarded live execution for Polymarket's 5-minute
BTC/ETH/SOL/XRP "up or down" markets. One question end-to-end: *is there a
replicable, low-capital edge, and what execution captures it?*

**Answer so far (from our own winner forensics + live probes):** the wallets
that win are set-traders — they **mint** outcome pairs at exactly $1.00
(CTF `splitPosition`), quote **both sides** as makers all window (zero maker
fee + rebates), and **merge** leftovers back to $1. Not signals, not speed:
inventory mechanics. Everything below serves validating and running that.

---

## 1. The Ireland box (the only machine that may trade)

| | |
|---|---|
| Host | `ubuntu@3.254.130.64` (AWS eu-west-1 — Polymarket's recommended region) |
| SSH | `ssh -i ~/.ssh/pm_deploy ubuntu@3.254.130.64` |
| Code | `~/project-fail` (deployed by `scp` from the local repo — **not** a git checkout) |
| Python | `~/project-fail/.venv/bin/python` (always use the venv binary) |
| Measured latency | REST to CLOB **28 ms** median; ws book events ~6 ms apart |

**The UK/local machine is paper-and-tooling only** (trading from the UK is
blocked). The geo interlock (`DEPLOY_REGION=eu-west-1` in the box `.env`)
hard-blocks every place-mode component anywhere else.

### tmux sessions on the box

| Session | Command it runs | What it is |
|---|---|---|
| `paper` | `python -m paper.run` | the 49-arm paper A/B (writes `paper/paper.db`) |
| `shadow` | `LIVE_EXECUTOR_MODE=shadow python -m live.executor` | executor soak: real client + feeds, orders suppressed |
| `mintbot` | `MINTBOT_MODE=shadow python -m live.mintbot` | mint/quote/merge bot (shadow until user launches place) |

View: `tmux attach -t paper` (detach `Ctrl-b d`) or `tmux capture-pane -t paper -p | tail -20`.

### The brake

```bash
touch ~/project-fail/paper/KILL
```
Every component checks this file each loop: the executor cancels everything and
exits; the mintbot cancels all asks and exits (minted sets are safe — they merge
or auto-redeem); the paper gate stops emitting intents. Remove the file before
restarting anything.

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

**Benchmark vs real winners** (local machine — needs local ClickHouse):
```bash
python winner_bench.py --hours 1
```
Prints the real-wallet pool/best/median for the same windows next to our arms.

**Other tools**: `tools/latency_probe.py` (run on box: RTT + ws cadence +
pipeline budget), `tools/locktaker_census.py` (local: same-block both-sides
buys = lock-taker competitor fingerprints), `live/credtest.py` (read-only
4-step credentials test), `live/balance.py` (wallet balances via RPC).

---

## 3. Strategy roster (49 paper arms + accumulators)

Everything races simultaneously in `paper/run.py::STRATEGIES` on the same tape.
Prefixes are composable lenses over the same signals:

| Prefix | Meaning |
|---|---|
| *(none)* | original research arm, f-skim fills (f=0.2 of each print) |
| `xf_` | exit-first twin: asks anchored at avg entry +2¢, forced taker dump last 40s |
| `ta_` | time-aware fair (`fair_up_t`: tanh, scaled by √(time left)) + late-floor |
| `lv_` | **live-mechanics twin**: $5 clip orders, order-sized fills both sides, G13 spend mirror, $50 cost cap — the **parity gate**: an arm goes live only after its `lv_` twin is green |
| `sq_` | slow-quote reference (requote 1.0 s = old file/poll pipeline) for the 4 live candidates |
| `mint_` | **mint-basis**: 20 sets minted at $1.00 flat, never bids, asks track fair per side, matched leftovers merge at $1 (the winners' mechanic) |

Signals: `twap` (Chainlink TWAP vs window ref), `binance`/`deribit` (external
spot), `mid` (market mid = "neutral"), `pair` (1 − other side's last price),
`pair_hl` (1 − other side's **best ask** — beat last-price anchoring by ~+84
over 52 windows), `twap_t` (time-aware). Confirm lists gate entries on feed
agreement. Board default requote: 0.15 s (`PAPER_REQUOTE_S`, probe-measured
in-process cadence).

Accumulators (not arms): `lock_arb`/`split_sell` (book-crossing set arb at 1.0 s
persistence = today's pipeline) and `lock_fast`/`split_fast` (0.15 s = in-process
pipeline). **Verdict from live probes: the taker-arb edge is ≈ zero** — the
lockbot went 0-for-lifetime on completed locks and was retired.

Key races the report prints: every `X vs xf_X / lv_X`, `sq_ vs lv_` (value of the
fast pipeline), `pair_mm vs hl_pair vs mint_hl` (entry mechanics), `mint_hl vs
mint_tw` (ask anchor).

### Fill-model honesty (hard-won)
Fills execute **at our posted quote** (tick-quantized), never at the aggressor's
print price; live_sim sells absorb whole prints up to inventory (order-sized);
settlements skip (never fabricate) on missing feed refs; mint windows always
record. Report `budget$` = peak simultaneous capital incl. mint outlay;
redemption lock measured ≈ 5–10 min (`REDEMPTION_LOCK=600`).

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
Enabled strategies: `paper/live.json`. Pipeline latency ~1 s end-to-end —
fine for makers, wrong for takers (why lockbot/mintbot are in-process instead).

### `live/mintbot.py` — mintbot v2 (in-process; the strategy that matters)
Per window per asset: `splitPosition` mints $20 of sets in the first 60 s →
maker asks on both sides, anchored `1 − opposite_ask + 2¢`, **joint-sum floor
1.005** (never sell a set below cost), books must be <3 s fresh → fills tracked
from the **data-api positions of the virgin minter EOA** (absent = unknown,
never zero) → T−20 s: cancel asks, re-poll, `mergePositions` the matched
remainder → single-side residue auto-redeems. Guards M1–M9 (geo, KILL +
cancel-all on every exit + startup sweep, $20/window + $250/day pessimistic
caps, first-60s-only fresh-clock mints, balance-revert ≠ infra failure, infra
merge-fail halt, allowance/balance preflight, quote sanity, positions truth).
Review-hardened: 6-agent pass, 15 confirmed findings fixed (gen-11), plus an
independent Codex xhigh pass on the chain layer.

### `live/chain.py` — Polygon layer (no web3 dependency)
Raw JSON-RPC + eth_account. RPC pool w/ fallback (reads) but **single-attempt
broadcasts** with honest semantics: `PreflightError` = $0 moved (estimateGas
stage), `BroadcastUncertain` = may be in mempool, reconcile via balances.
Nonce-locked sends (4 assets share close boundaries). Minimal ABI encoder
(static types + one `uint256[]`, guarded). Contracts: CTF
`0x4D97...6045`, USDC.e `0x2791...4174` (the collateral — **not** native USDC),
CTFExchange `0x4bFb...982E`.

### Minter wallet (EOA — separate from the site account)
`0xbb791E91F284E077a0a848C821690BB6A2dcfda7` — fresh MetaMask account holding
USDC.e + POL, keys only in the box `.env`. Why an EOA: the site's proxy wallet
can't call `splitPosition` itself. One-time scripts (user-run):
`live/minter_setup.py` ($1 mint→merge proof — **PASSED 2026-08-24 21:41Z**),
`live/minter_approve.py` (exchange approval + $50k USDC.e allowance).

### `live/lockbot.py` — RETIRED
In-process taker set-arb. Lifetime: ~10 detections, 0 completed locks, ~$4.35
tuition. Lessons that now protect the mintbot: marketable-order amount rules
(2-decimal dollars, $1/leg minimum), fill→sellable indexing lag, adverse
selection on crossed books. Kept in-repo as reference.

---

## 5. Operating rules (standing, user-set)

1. **Parity rule**: nothing changes in production without paper first; an arm
   goes live only when its live-mechanics twin is green.
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

### Launch commands (user-run, in order)
```bash
# one-time approvals (~1c gas)
ssh -t -i ~/.ssh/pm_deploy ubuntu@3.254.130.64 "cd ~/project-fail && ./.venv/bin/python -m live.minter_approve"
# mintbot live ($20/window x 4 assets, $250/day, KILL-file brake)
ssh -t -i ~/.ssh/pm_deploy ubuntu@3.254.130.64 "tmux kill-session -t mintbot; cd ~/project-fail && tmux new -d -s mintbot 'MINTBOT_MODE=place ./.venv/bin/python -m live.mintbot 2>&1 | tee -a live/mintbot.log' && sleep 8 && tail -3 live/mintbot.log"
```

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

Headline verdicts to date: regime seesaw dominates directional arms; carry is
the killer (small clips + exits beat carry by hundreds of $ per family per
chop-hour); the fast pipeline is worth +10…+54/family/5 h vs 1 s quoting;
taker set-arb ≈ dead; mint entry beats book entry (+13 on first same-tape race);
the neutral/pair relaunch gate remains **closed**.

---

## 7. History: the incident that shaped the guards

First live session (gen-3 era) lost ≈ $10 in 15 min because the executor
compared its **signer** address against fills that carry the **funder/proxy**
address → ledger blind → inventory cap never engaged → ~130 stale $5 bids
refilled on falling sides. Every guard since — authoritative balance/positions
data plane, placement-time caps, day-stop on wallet drift, KILL, parity rule,
paper-first — exists because of it. The trades feed multi-counts maker fills;
only balances and positions are truth.
