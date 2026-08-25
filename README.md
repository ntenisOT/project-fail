# project-fail — Polymarket 5-minute crypto trading system

Research → paper A/B → guarded live execution for Polymarket's 5-minute
BTC/ETH/SOL/XRP "up or down" markets. One question end-to-end: *is there a
replicable, low-capital edge, and what execution captures it?*

**Status: NO-GO for real money.** V2-corrected winner forensics invalidated the
old mint-and-sell taxonomy. The clean leader rests bids on both outcome tokens,
acquires fee-inclusive pairs below $1, never sells, and holds to settlement.
Other winners add directional exposure, but trading both tokens does not prove
simultaneous asks or direct CTF minting. Mint-and-ask is not the winner replica.

---

## 1. The Ireland box (paper monitoring only)

| | |
|---|---|
| Host | `ubuntu@3.254.130.64` (AWS eu-west-1; paper/tooling only) |
| SSH | `ssh -i ~/.ssh/pm_deploy ubuntu@3.254.130.64` |
| Code | `~/project-fail` (deployed by `scp` from the local repo — **not** a git checkout) |
| Python | `~/project-fail/.venv/bin/python` (always use the venv binary) |
| Measured latency | CLOB `/time` GET **27–32 ms** median / **31–70 ms** p90 across probes; ordinary WS event age **8–13 ms** p50 / **9–24 ms** p90 with separate observed 0.4–3.8 s tails; paper action proxy **65 ms** (authenticated POST/cancel unmeasured) |

**Both machines are paper-and-tooling only.** On 2026-08-25 Polymarket's live
`/api/geoblock` endpoint returned `blocked=true` for both the UK/local machine
(`GB`) and this Ireland box (`IE`). Polymarket's current documentation is
internally inconsistent: the developer geoblock page classifies Ireland as
frontend-only and calls `eu-west-1` the closest non-georestricted server region,
while the Help Center lists Ireland as fully blocked. `DEPLOY_REGION=eu-west-1`
is only a topology label; it is not proof of user eligibility and must never
authorize order placement. Every place-mode component remains hard-disabled.
A future launch requires an unblocked live check, independently confirmed
physical-location eligibility, and resolution of any documentation conflict;
servers, VPNs, or proxies must not be used to circumvent a restriction.

### tmux sessions on the box

| Session | Command it runs | What it is |
|---|---|---|
| `paper` | `python -m paper.run` | five queue-aware paired-bid hypotheses (writes `paper/paper.db`) |
| `mintbot` | stopped; on-demand `MINTBOT_MODE=shadow MINTBOT_ASSETS=btc python -m live.mintbot` | do not duplicate paper's BTC feed during primary experiments; place mode is forbidden |

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
mark, isolated outcome luck, worst-case PnL, pair sums, FIFO completion delay,
queue depth, residence, official 60-second Chainlink TWAP shadow coverage, and
structured invalid-window reasons/exposure:
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
official resolved slugs, full token lifecycles, per-token inventory, explicit
taker fees, and read-only ClickHouse external tables. The normalizer handles
CLOB V2's per-order fill events: the taker summary has the exchange contract as
counterparty and must not be expanded as a second bilateral trade. The old V1
expansion fabricated sells, cycles, and maker roles from complementary buys.
The tool separates direct CTF events and reports fee-inclusive pair proxies.
Its `pnl$` excludes separately paid maker rebates; query the official rebate
endpoint before comparing total wallet economics. Paper's `rebate$` is only the
current documented 20% fee-equivalent baseline because actual pool payouts can differ.
`tools/latency_probe.py` measures GET and feed surfaces without calling an order
endpoint, then compares the configured paper delay with twice the GET p90 proxy.
`tools/order_latency_probe.py` is a separate dry-run-by-default, account-owner-run
diagnostic for one bounded post-only order, targeted cancellation, and final
zero-open-order verification; it never enables a strategy or cancels prior orders.
`tools/wallet_timing.py` tests the stronger all-window-maker claim by separating
pre-event, early, middle, late, and post-event volume, maker share, and full-span
market participation for explicitly supplied wallets. `tools/wallet_cycles.py`
requires opposite-token FIFO round trips with overlapping holding intervals in
exact block/log order, then reports cycle sums, edge, coverage, and holding
time; it also nets same-token alternating inventory without double-counting and
does not treat uncovered sells as proof of minting. `tools/wallet_signal.py`,
`tools/wallet_tape.py`, and `tools/wallet_markout.py` separate neutral pair edge
from outcome alignment and screen public-tape signals after slippage and fees.
`tools/wallet_pairs.py` matches opposite-token buys FIFO in exact block/log
order so aggregate token averages cannot disguise completion coverage, pair-cost
distribution, residual imbalance, or pairing delay.

Latest bounded refresh separates two regimes. During 2026-08-23 15:25 through
08-24 15:20 UTC, `0xb27…` made +$18,844 on $1.28m as a 99.5%-maker paired-bid
accumulator: 96.8% completion, $0.980 average pair, and 9 s / 43 s FIFO d50/d90.
Its neutral mechanics made about +$30,251 while direction cost about $11,106.
During the following 24 hours, `0x0cb…` made +$5,894 on $516k primarily from
early directional flow: about +$15,858 direction versus -$9,963 neutral. Its
T+30/T+60 calls underperformed the contemporaneous public favorite. A public
T+60 favorite screen changed from -3.83 cents/share in the prior day to +3.89
cents/share in the current day after optimistic 1% slippage and modeled fees.
There is no durable fixed momentum rule, no observed direct split value at these
trading addresses, and no single strategy shared by every current PnL leader.
See `reports/2026-08-25-gen62-winner-regimes.md`.

---

## 3. Focused pair-inventory experiment (six strategies)

The legacy 49-arm board was retired after every execution-shaped pair/mint arm
remained negative. V2-corrected forensics then identified the clean leader as a
paired-bid accumulator. Strict inside-$0.98 won the first price-priority screen.
Basket99 later reached winner-like pair cost/completion timing but left
outcome-risk residue. The current board isolates late pair creation, mint churn
count, and fee-aware completion:

| Strategy | Mechanic |
|---|---|
| `basket99` | Five-share maker baseline; keep cumulative completed-pair average ≤$0.99 |
| `basket99c180` | Basket99 twin that stops opening fresh pairs after T+180 but can finish an existing leg |
| `basket99t270` | Basket99 twin that waits until T+270, then takes only a displayed complement whose fee-inclusive cost preserves the rolling $0.99 cap |
| `mintcycle5` | One five-share complete-set pair per window; tests whether repeated mint churn compounds residue risk |
| `mintcycle20` | Queue-aware $20 complete-set control: paired maker asks with a $1.005 joint floor, realistic 65 ms actions, and T+30/T+240 entry bounds |
| `mintrepair5p95` | Five-set mint twin that gives maker completion 60 seconds, then reduces executable residual depth only above a fee-inclusive $0.95 pair floor; near-minimum dust may round up by at most 0.1 share |

The observer records the official
`crypto_prices_twap_sixty` RTDS stream as a **shadow-only** reference. It stores
the exact E18 value plus observation and local-receive timestamps, reconstructs
only samples causally available at T+30, and reports every missing or late
opening observation. It has no path into quote generation. The legacy spot
`crypto_prices_chainlink` topic is not the settlement reference for current
five-minute crypto markets ([Chainlink TWAP](https://docs.polymarket.com/market-data/chainlink-twap),
[prediction changelog](https://docs.polymarket.com/changelog/predictions)).

Market discovery carries each market's Gamma `orderMinSize` into maker and
taker simulation. A partial residual below that share minimum cannot normally
be posted or force-filled. The repair arm may submit exactly the minimum only
when the open residual is within 0.1 share of it, deliberately flipping at most
0.1 share of dust instead of retaining almost five shares. Five-share low-price
orders are not incorrectly rejected on dollar notional.

All six arms wait until T+30 seconds and until both token feeds have caught up
before starting a pair. This deliberately gives up the subscription-backlog
period; a stale causal update freezes decisions and labels exposed settlement
economics `lagged` rather than removing the window.

The simulator reconstructs public price levels from authoritative snapshots and
`price_change` deltas. Joining the best price puts the displayed level size in
front of our hypothetical order; trades consume that queue before we receive a
fill. Same-price quotes retain queue position, repricing loses it. The five-share
arms are reported independently; taker hedges pay the documented crypto fee,
while projected maker rebates are reported but excluded from PnL. Official
resolved Gamma outcomes settle each window. A window whose first usable
paired books
arrive more than ten seconds late is observed but not scored. Actions activate
after a configurable 65 ms delay: a fixed ordinary-path proxy, not a measured
authenticated POST/cancel distribution. Existing orders can
still fill while a delayed cancellation is in flight, and stale post-only
replacements are rejected. Once one token fills, its exact open-leg price caps
or floors the opposite-token quote; the reported pair sums are FIFO-matched
fills rather than same-time quote sums. Any market-WebSocket disconnect
invalidates the active window because missed trades cannot be reconstructed. A
`book` or `price_change` event arriving more than `PAPER_MAX_EVENT_LAG_MS`
(400 ms by default) late freezes new decisions for its affected asset until both
token streams catch up. Existing hypothetical exchange orders remain exposed and
ordered delayed trades can still fill them; this models measured latency instead
of censoring it. Settlement reports split these windows into `lagged` and `clean`
economics. A delayed `last_trade_price` does not freeze a fresh book, but its
late fill awareness independently marks exposed windows `lagged`. Heartbeats
report rolling server-event p50/p90/max lag, stale/delayed-trade event counts,
reconnect count, and the ordered feed-queue high-water mark. Socket reads are
decoupled from event processing through a bounded 8,192-event queue; local queue
delay still appears in event age, while overflow or disconnect invalidates the
window. Market rolls use the official in-place subscription update protocol:
the new token pair is subscribed before the old pair is unsubscribed, preserving
one socket instead of forcing a reconnect and duplicate snapshot burst every
five minutes ([market channel](https://docs.polymarket.com/api-reference/wss/market)).
The ledger separately persists every truly invalid
strategy-window, its reason, fills, peak committed capital, cash, and residual
inventory. Reports include the cohort validity rate so rejected windows cannot
silently disappear from the denominator. Completed opposite-token fills retain
share-weighted FIFO d50/d90 timing for direct comparison with winner wallets.

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

### `live/mintbot.py` — legacy mint hypothesis (shadow only)
Per window per asset: `splitPosition` mints $20 of sets in the first 60 s →
maker asks on both sides, anchored `1 − opposite_ask + 2¢`. It consumes
`price_change` deltas, sends the required heartbeat, verifies the old pair is
gone before one batch replacement, posts five-share clips, preserves queue
priority with a five-tick/15-second band (10-tick adverse override), and
stops quoting after asymmetric fills. Both token caches must have their own
timestamped snapshot/delta update within `MINT_BOOK_FRESH_MS` (2,000 ms by
default); unrelated token traffic can no longer make a stale pair look healthy.
The mintbot now shares paper's bounded ordered feed pump, reports queue high
water and separately counts deltas that actually updated its best-ask cache.
A bounded post-fix soak proved those updates, then revealed that running paper
and mintbot as separate subscribers to the same BTC pair can make one socket
receive a server `1013` at the window burst even with shallow local queues.
Mintbot is therefore stopped during paper experiments; a future concurrent
design must fan out one owned feed locally rather than open duplicate sockets.
A joint-sum
floor constrains a quoted pair but cannot guarantee paired fills. Position
polling still cannot reconstruct exact fill prices, so reported PnL is not yet
authoritative. `mint_cycle20` shares the same anchor and residence policy but,
unlike the shadow bot, uses immediate simulated fills to complete an asymmetric
clip. The shadow bot still stops after an inferred imbalance because its delayed
position poll cannot prove fill price or order ownership. Corrected V2 wallet
forensics no longer identifies mint-and-ask as the clean leader, so this is not
the winner-replication path. Keep it out of place mode unless direct CTF events
and execution-normalised economics independently restore the thesis.

### `live/chain.py` — legacy direct-EOA Polygon layer
Raw JSON-RPC + eth_account, retained for read-only balances and migration
forensics. It predates this repo's adoption of Polymarket's official Builder
Relayer and is no longer the intended mint execution design.

### Minter wallet (EOA — separate from the site account) — DEPRECATED PATH
`0xbb791E91F284E077a0a848C821690BB6A2dcfda7` — separate EOA, keys only in the
box `.env`, holding **891.930536 legacy USDC.e**, zero pUSD, and POL
(consolidating it is a user money-moving decision, not a software task). The
site/CLOB Safe currently holds **9.855666 pUSD**. Legacy USDC.e must be migrated
before it can serve as CLOB V2 collateral.

**Architecture correction (2026-08-25):** the separate EOA was never a protocol
requirement. Polymarket's official Relayer executes gasless split / merge /
redeem / approvals **from the Safe/Proxy wallet itself**, and CLOB orders use
that same wallet as funder (docs: trading/gasless, market-makers/getting-
started). We built the EOA + raw-RPC path only because our code lacked Builder
Relayer integration — an implementation shortcut, stated at the time as "the
proxy cannot mint," which was wrong. **Decision: do not repair the direct-EOA
place path.** If minting ever becomes evidence-backed, rebuild the thin
on-chain part around the existing Safe + official Relayer (one account, one
inventory ledger: `POLY_FUNDER Safe → Relayer split/merge/redeem → CLOB
quote/fill`). Blockers for that route today: no Relayer/Builder API credentials
on the box, and the USDC.e→pUSD collateral migration. The old direct-CTF
scripts stay disabled; mintbot place mode stays hard-disabled in code.

### `live/lockbot.py` — RETIRED
In-process taker set-arb. Lifetime: ~10 detections, 0 completed locks, ~$4.35
tuition. Lessons that now protect the mintbot: marketable-order amount rules
(2-decimal dollars, $1/leg minimum), fill→sellable indexing lag, adverse
selection on crossed books. Kept in-repo as reference; place mode now
hard-refuses before constructing a client or touching its database.

---

## 5. Operating rules (standing, user-set)

1. **Eligibility and parity rule**: no order path may run unless Polymarket's
   live geoblock check and the user's physical-location eligibility both pass.
   Nothing changes in production without paper/replay first.
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
   commit+push to `main`. Bind `PAPER_ASSETS`, action latency, and stale-event
   threshold explicitly in the tmux command, then verify the startup log.
   Reports are sorted by PnL and retain queue/inventory evidence.

### Launch commands

No place-mode or approval command is published while the launch gate is closed.

### `.env` on the box (names only — never commit values)
`POLY_PRIVATE_KEY`, `POLY_FUNDER`, `POLY_SIGNATURE_TYPE` (site/CLOB identity) ·
`MINTER_PRIVATE_KEY`, `MINTER_ADDRESS` (mint EOA) · `DEPLOY_REGION=eu-west-1`
(legacy topology label, **not** an eligibility check) · `TELEGRAM_*` (reports) ·
`PAPER_SUMMARY_MINS`, `PAPER_ASSETS`,
`PAPER_ACTION_LATENCY_MS`, `PAPER_MAX_EVENT_LAG_MS` ·
optional `POLYGON_RPC_URL`,
`MINT_USD`, `MINT_DAY_CAP`, `MINT_SPREAD`, `MINT_BOOK_FRESH_MS`,
`MINTBOT_ASSETS`, `LIVE_EXECUTOR_MODE`, `MINTBOT_MODE`.

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
| 24 | 08-25 04:xx | match trades at exchange event time and reject delayed prints that predate simulated order activation |
| 25 | 08-25 04:xx | align paper and mintbot discovery wakeups to exact five-minute boundaries instead of fixed-loop phase |
| 26 | 08-25 04:xx | replace unconditional 40-second taker cleanup with a fee-net pair-floor guard |
| 27 | 08-25 04:xx | move blocking Telegram startup/report calls off the market-feed event loop |
| 28 | 08-25 04:xx | reject dominated timed taker completion; stop new mint pairs at T-60 while completing open legs |
| 29 | 08-25 04:xx | raise the bounded market-frame burst buffer after queue-64 slow-consumer disconnects |
| 30 | 08-25 05:xx | replace disproven T-60 cutoff with immediate fee-aware complementary-depth flattening |
| 31 | 08-25 05:xx | start hedge reaction latency at local fill receipt while retaining exchange-time maker causality |
| 32 | 08-25 05:xx | combine initial complete-set inventory with balanced maker sell-and-replenish churn |
| 33 | 08-25 05:xx | phase inventory sell and replenishment cycles so crossed partial fills cannot deadlock accounting |
| 34 | 08-25 05:xx | move per-fill SQLite commits to a single owned writer thread after another paper-only feed stall |
| 35 | 08-25 05:xx | A/B phased inventory against continuous two-sided complete-set market making; retire rejected taker flattening |
| 36 | 08-25 06:xx | forbid inventory filtering from turning a guarded pair start into a loss-making single-leg quote |
| 37 | 08-25 06:xx | A/B strict complete-set inventory at join-best versus one-tick-inside prices |
| 38 | 08-25 06:xx | replace V1-corrupted mint/churn thesis with V2-correct paired-bid accumulation; 2×2 price-priority test |
| 39 | 08-25 07:xx | retain strict inside-$0.98 control; compare rolling basket-average caps after winner pairing-delay and sizing reconstruction |
| 40 | 08-25 07:4x | reject four-asset and initial BTC-only samples after causal feed lag exceeded 400 ms without a disconnect |
| 41 | 08-25 07:5x | pause through the repeatable subscription backlog; start BTC pairs at T+30 only after both token feeds catch up |
| 42 | 08-25 08:15 | score feed validity per strategy and include resting-bid collateral in capital; continue the BTC basket control |
| 43 | 08-25 08:34 | persist invalid-window exposure and trigger lag; report FIFO pair d50/d90; a wrong all-asset bootstrap was caught before T+30 and replaced by the verified BTC-only runner |
| 44 | 08-25 08:47 | stop censoring measured public-feed tails: freeze decisions, retain resting exposure and delayed ordered trades, then report clean versus lagged economics separately |
| 45 | 08-25 08:54 | include delayed trade/fill awareness in the lagged quality class after Gen44 observed a 1.716 s trade tail that causal-book-only labeling would have missed |
| 46 | 08-25 09:11 | replace the nearly redundant Basket985 arm with 5-share versus 10-share Basket99 twins and a fee-aware T+120 taker-completion twin; retain strict and Basket98 controls |
| 47 | 08-25 09:49 | reject the 10-share arms after five windows; keep 5-share Basket99 control and isolate a T+180 new-pair cutoff from fee-aware completion at T+120/T+180 |
| 48 | 08-25 10:05 | retain Basket98/Basket99/cutoff controls; replace redundant taker twins with a stable two-level replenishing ladder and an honest queue-aware mint-cycle control |
| 49 | 08-25 10:33 | preserve the Gen48 board; decouple WebSocket draining from ordered processing after another real 1013 slow-consumer disconnect; share the pump with mintbot and require fresh timestamped updates for each outcome token |
| 50 | 08-25 10:44 | preserve the board; stop duplicate shadow subscriptions and rotate market tokens in-place on one persistent official WebSocket instead of reconnecting at every boundary |
| 51 | 08-25 11:00 | preserve the board; prove stale-count/heartbeat discrepancies are timestamped feed tails hidden by the 4,096-event rolling display, not missing timestamps |
| 52 | 08-25 11:16 | measurement-only reset: conserve each public trade's size across ladder lanes and cap through-price fills by observed print size; report interval/lifetime feed peaks that cannot age out before heartbeat |
| 53 | 08-25 11:31 | preserve the corrected board; timestamp pump enqueue/handler residence to split upstream event age from local queue/processing delay |
| 54 | 08-25 11:49 | persist feed lifetime counters across socket generations; retire the failed ladder and add a bounded 60-second, $0.95 fee-inclusive mint-repair A/B arm |
| 55 | 08-25 12:05 | pre-fix diagnostic: expose sub-minimum mint residue; replace the false fixed-$1 notional rule with each market's official share minimum |
| 56 | 08-25 12:17 | first corrected-minimum cohort: two profitable sell pairs were overwhelmed by one unmatched five-share winning liability; hedge encountered both depth and price blocks |
| 57 | 08-25 12:33 | measurement-only reset: retain all parameters and record the best executable fee-inclusive repair sum after the hedge timer |
| 58 | 08-25 12:54 | retain the board; attribute stale and delayed events only when their exchange timestamp overlaps an actual order, pending action, or unpaired leg |
| 59 | 08-25 13:11 | retire the repeatedly unproductive Basket98 arm; compare one-pair versus four-pair mint churn and enforce market share minimums on maker posts |
| 60 | 08-25 13:40 | preserve the strategy board; add share-weighted maker fill age and signed 1/5/15-second midpoint markouts before testing repricing or a signal gate |
| 61 | 08-25 14:55 | preserve all trading behavior; collect the official 60-second Chainlink TWAP causally at T+30 and audit exact-opening coverage before designing any signal gate |
| 62 | 08-25 15:27 | decouple official market outcomes from strategy validity so reconnect-invalidated windows remain usable for the shadow-signal audit; no quote changes |
| 63 | 08-25 15:51 | add a T+270 fee-aware Basket99 completion A/B and replace the inert full-depth mint hedge with a five-set partial-depth/dust repair twin; keep signal shadow-only |

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
