# project-fail — Polymarket 5-minute crypto: research + paper trader

Research pipeline and a **paper-only** trading harness for Polymarket's 5-minute
BTC/ETH/SOL/XRP "up or down" markets. It answers one question end-to-end: *is there
a replicable, low-capital edge, and if so, what execution captures it?*

> **Paper only.** Nothing here places a real order, signs a transaction, or touches
> a wallet or private key. Live execution is deliberately out of scope.

## What the research found
- These markets settle on the **Chainlink 60-second TWAP** (not spot).
- No naive directional edge — ~95% of windows are decided by 30s to close.
- The wallets that win are **high-frequency market-makers**; return on *capital*
  (not volume) is large and low-capital.
- A real, mechanical signal exists: **the Chainlink TWAP leads the market price**
  (a passive MM priced off the TWAP was net-positive in simulation vs. a market-mid
  baseline). Whether it survives real fills is what the paper trader measures.

## Components
| File | Purpose |
|---|---|
| `recorder.py`, `recorder_v2.py` | live order-book + Chainlink/Binance recorders |
| `backtest.py` | directional-rule backtester on historical trades |
| `winners.py`, `cohort.py`, `winners_sizing.py`, `profile.py` | winner-wallet analysis (P&L, style, sizing, stop-loss) |
| `capital.py` | return-on-capital estimator |
| `mm_sim.py`, `mm_sim_v2.py` | market-making simulators |
| `paper/` | the live **11-strategy paper-trading A/B harness** |


## Fill model v2 + live-readiness (2026-08-23)

- `paper/engine.py` fill model v2: fills hit POSTED quotes up to `PAPER_REQUOTE`
  (1.0s) stale -> adverse selection simulated; quotes only post with >=5-share
  capacity/inventory (Polymarket limit-order minimum); maker fee 0 per market
  metadata (`crypto_fees_v2` is taker-only; maker rebates ignored = conservative).
- `paper/live_gate.py` + `GO_LIVE.md`: per-strategy graduation path. Strategies
  emit desired-quote INTENTS (paper/intents.jsonl) behind a double opt-in
  (PAPER_LIVE_INTENTS=1 + paper/live.json) with hard caps and a KILL file.
  No keys/signing/submission in this repo - a user-run executor consumes intents.
- v1 optimistic-model ledger archived as paper/paper_fillv1_20260823.db.

## Paper trader (`paper/`)
Runs 11 strategies in parallel on the same live feed, each with its own ledger:
hold, roundtrip, rt_wide, opp_size, **neutral** (no-signal control), lock_arb, and a
signal decomposition (twap / binance / deribit / confirmations). Fair value is
computed from Chainlink TWAP + Binance + Deribit (**signal only** — no capital off
Polymarket) and passed into a two-sided round-tripping MM engine.

```bash
pip install -r requirements.txt
python -m paper.run            # runs the A/B; logs a comparison every 15 min
python -m paper.report         # print the current A/B comparison
```
Optional env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_PUSH_CHAT_IDS`, `PAPER_ASSETS`.

## Notes
- Research scripts read a local ClickHouse of Polymarket trade history; connection
  defaults to `localhost` dev credentials — move to env before any non-local use.
- Requires Python 3.13+.
