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
| Measured latency | Repeated CLOB `/time` GET **27–28 ms** median / **31–33 ms** p90; legacy 1 s poll added **546 ms** median / **928 ms** p90 and has been removed; paper action proxy **65 ms** (authenticated POST/cancel unmeasured) |

**The UK/local machine is paper-and-tooling only** (trading from the UK is
blocked). The geo interlock (`DEPLOY_REGION=eu-west-1` in the box `.env`)
hard-blocks every place-mode component anywhere else.

### tmux sessions on the box

| Session | Command it runs | What it is |
|---|---|---|
| `paper` | `python -m paper.run` | four queue-aware pair/inventory hypotheses (writes `paper/paper.db`) |
| `mintbot` | `MINTBOT_MODE=shadow python -m live.mintbot` | feed/quote soak only; place mode is currently forbidden |

View: `tmux attach -t paper` (detach `Ctrl-b d`) or `tmux capture-pane -t paper -p | tail -20`.

### The brake

```bash
touch ~/project-fail/paper/KILL
```
Every component checks this file: the executor cancels everything and exits;
the mintbot cancels all asks and exits (balanced pairs can merge, but any
unpaired residue remains outcome risk); the paper simulator exits.
Remove the file before restarting anything.

---

## 2. Getting status and reports

All read-only; run from anywhere with the SSH key.

**Focused paper report** — realized PnL, paired edge, neutral 50-cent inventory
mark, isolated outcome luck, worst-case PnL, pair sums, queue depth, and residence:
```bash
ssh -i ~/.ssh/pm_deploy ubuntu@3.254.130.64 'cd ~/project-fail && ./.venv/bin/python -m paper.report'
```

**Heartbeat** (runner, feed events, fills, resting paper orders and pending official outcomes):
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
`PAPER_SUMMARY_MINS` minutes (monospace, sorted by pnl) — configured via `.env`.

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
`tools/latency_probe.py` measures GET and feed surfaces without calling an order
endpoint, then compares the configured paper delay with twice the GET p90 proxy.
`tools/wallet_timing.py` tests the stronger all-window-maker claim by separating
pre-event, early, middle, late, and post-event volume, maker share, and full-span
market participation for explicitly supplied wallets. `tools/wallet_cycles.py`
requires opposite-token FIFO round trips with overlapping holding intervals in
exact block/log order, then reports cycle sums, edge, coverage, and holding
time; it does not treat uncovered sells as proof of minting.

---

## 3. Focused pair-inventory experiment (four strategies)

The legacy 49-arm board was retired after every execution-shaped pair/mint arm
remained negative and corrected wallet forensics showed that “both tokens” is a
signature, not one universal mechanism. The current board tests only:

| Strategy | Mechanic |
|---|---|
| `pair_churn20` | Join both best bids when their sum is ≤0.99, then offer balanced inventory when the ask sum is ≥1.01 |
| `pair_inside20` | Improve both maker prices by one tick when pair sums remain ≤0.99/≥1.01, trading margin for queue priority |
| `mint_cycle20` | Minted inventory with opposite-ask +2¢ anchors; after one side fills, quote only the other side above the realized pair floor before starting another clip |
| `mint_hedge40` | Same mint cycle, but after 40 seconds an unmatched leg is sold through displayed bid depth with official taker fees |

The simulator reconstructs public price levels from authoritative snapshots and
`price_change` deltas. Joining the best price puts the displayed level size in
front of our hypothetical order; trades consume that queue before we receive a
fill. Same-price quotes retain queue position, repricing loses it. All strategies
use five-share clips, maker fees/rebates are excluded, and official resolved
Gamma outcomes settle each window. A window whose first usable paired books
arrive more than ten seconds late is observed but not scored. Actions activate
after a configurable 65 ms delay: approximately twice
fresh Ireland GET p90 as a conservative cancel/replace proxy. Existing orders can
still fill while a delayed cancellation is in flight, and stale post-only
replacements are rejected. Once one token fills, its exact open-leg price caps
or floors the opposite-token quote; the reported pair sums are FIFO-matched
fills rather than same-time quote sums. Any market-WebSocket disconnect
invalidates the active window because missed trades cannot be reconstructed;
heartbeats report rolling server-event p50/p90/max lag and reconnect count.

This is substantially less optimistic than the retired print-skimming model,
but still cannot model private cancellations ahead of us, exchange order
acknowledgement, authenticated fills or our own impact. It remains a rejection
and comparison tool, not production-parity proof.

---

## 4. Live components

### `live/executor.py` — quote executor (file-driven)
Legacy component that consumes `paper/intents.jsonl`. The focused runner emits
no intents, so it is disconnected and must remain off. Modes
`log-only` / `shadow` / `place`. Guards G1–G17: geo interlock, startup/exit
cancel-all, per-action try/except, window-end cancels, $5 order cap,
holdings-gated sells, session exposure cap, **balance-based day stop persisted
per UTC day** (`live/day_baseline.json` — trips on any wallet drift it didn't
cause, incl. manual trading on the same wallet), action budget, KILL, rotation-
safe reader, close-dump, placement-time spend cap with cancel refunds, post-only
throttle (10 s cooldown), ledger tripwire, pair-recycler + lock-taker (dormant).
The tracked `paper/live.json` enables no strategies. A fresh legacy quote-intent
probe measured 546 ms median / 928 ms p90 before REST. That delay came from the
one-second file poll; the focused paper runner neither emits intents nor starts
this executor.

### `live/mintbot.py` — experimental mintbot (in-process; shadow only)
Per window per asset: `splitPosition` mints $20 of sets in the first 60 s →
maker asks on both sides, anchored `1 − opposite_ask + 2¢`. It consumes
`price_change` deltas, sends the required heartbeat, verifies the old pair is
gone before one batch replacement, posts five-share clips, preserves queue
priority with a five-tick/15-second band (10-tick adverse override), and
stops quoting after asymmetric fills. A joint-sum
floor constrains a quoted pair but cannot guarantee paired fills. Position
polling still cannot reconstruct exact fill prices, so reported PnL is not yet
authoritative. `mint_cycle20` shares the same anchor and residence policy but,
unlike the shadow bot, uses immediate simulated fills to complete an asymmetric
clip. The shadow bot still stops after an inferred imbalance because its delayed
position poll cannot prove fill price or order ownership. Keep both out of place
mode until authenticated receipts and strategy edge are proven.

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
selection on crossed books. Kept in-repo as reference; place mode now
hard-refuses before constructing a client or touching its database.

---

## 5. Operating rules (standing, user-set)

1. **Parity rule**: nothing changes in production without paper/replay first.
   A green queue-aware paper arm alone is insufficient; order/fill/position reconciliation
   and measured execution behaviour must also pass.
2. **Every runner restart archives the DB**: verify the runner is DEAD first
   (`tmux kill-session -t paper`, then PID-checked wait), then
   `mv paper/paper.db paper/paper_genN_<date>end.db && rm -f paper/intents.jsonl`,
   then start fresh. Never mix generations in one sample.
3. **Launch boundary**: the agent prepares everything; **the user runs every
   place-mode launch and every command that moves money**. Keys go from
   MetaMask to the box `.env` by the user's hands only.
4. **Never `pkill` on the box.** The tmux server's argv contains session command
   strings — pattern kills murder every session (it happened three times).
   `tmux kill-session` + PID-targeted `kill` only.
5. Deploys: local edit → focused checks → `scp` → hash-verified restart →
   commit+push to `main`. Reports are sorted by PnL and retain queue/inventory evidence.

### Launch commands

No place-mode or approval command is published while the launch gate is closed.

### `.env` on the box (names only — never commit values)
`POLY_PRIVATE_KEY`, `POLY_FUNDER`, `POLY_SIGNATURE_TYPE` (site/CLOB identity) ·
`MINTER_PRIVATE_KEY`, `MINTER_ADDRESS` (mint EOA) · `DEPLOY_REGION=eu-west-1`
(geo interlock) · `TELEGRAM_*` (reports) · `PAPER_SUMMARY_MINS`, `PAPER_ASSETS`,
`PAPER_ACTION_LATENCY_MS` ·
optional `POLYGON_RPC_URL`,
`MINT_USD`, `MINT_DAY_CAP`, `MINT_SPREAD`, `LIVE_EXECUTOR_MODE`, `MINTBOT_MODE`.

---

## 6. Generations & DB archives

Every generation = one clean sample under one code state; archives live next to
the DB as `paper/paper_genN_<date>{start,end}.db`.

| Gen | When (UTC) | What changed |
|---|---|---|
| ≤5 | morning | research arms → fill model v2 → lv_ parity twins |
| 6 | 17:32 | full lv_ coverage (45 arms), hl_pair debut |
| 7 | 18:06 | **15 code-review fixes** (fills at quote, settlement honesty, executor guards) |
| 8 | 19:22 | lock/split 1 s persistence (kills flicker-arb fantasy) |
| 9 | 20:06 | whole board 0.15 s requote, sq_ slow twins, lock/split fast tiers |
| 10 | 21:53 | mint_hl / mint_tw debut + mintbot v1 shadow |
| 11 | 22:15 | **mintbot v2: 15 mint-stack review fixes**; paper mints 20 sets; order-sized sells restored; budget fix |
| 12 | 08-25 02:xx | four queue-aware pair/inventory hypotheses; official outcomes; measured 65 ms action delay |
| 13 | 08-25 02:4x | open-leg price constrains later pair completion; outcome side persisted per fill |
| 14 | 08-25 02:5x | delayed actions revalidate fills/holdings; negative inventory hard-fails; worst-outcome PnL |
| 15 | 08-25 02:18 | roll resubscribe bounded to 500 ms; 240 s cutoff rejected after 11 windows: $0.60 worse and more unmatched inventory |
| 16 | 08-25 02:4x | replace failed cutoff with five-second, displayed-depth FOK cleanup including crypto taker fees |
| 17 | 08-25 02:5x | replace intermittent one-second empty-token polling at market rolls with an event-driven wake-up |
| 18 | 08-25 03:xx | retire zero-edge taker hedge; add one-tick maker-priority churn and neutral/outcome PnL decomposition |
| 19 | 08-25 03:xx | replace fast best-ask mint proxy with the shared mintbot quote planner and residence policy |
| 20 | 08-25 03:xx | bound WebSocket close handshakes to 100 ms and remove mintbot's empty-token one-second poll |
| 21 | 08-25 03:xx | replace rejected asymmetric-stop mint paper arm with one-leg cycle completion; bound transient WebSocket retry from 100 ms |
| 22 | 08-25 04:xx | retire outcome-dependent carry; compare mint-cycle tail against 40-second depth-and-fee taker cleanup |
| 23 | 08-25 04:xx | invalidate reconnect-tainted windows; increase burst queue to 64 frames and measure server-event lag directly |

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
