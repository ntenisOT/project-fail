# Paper Trader — Requirements (5-min crypto TWAP market-making)

**Goal:** Live *paper*-trade the strategy against the real Polymarket book + Chainlink
TWAP, simulate fills, track P&L, and report to Telegram — to measure whether the
edge from mm_sim v2 (~10%/window, idealized) survives *real* fills and queue
competition. **Paper only: no order submission, no wallet, no private key, no money.**

## Why (the one question it answers)
mm_sim v2 showed a real signal (Chainlink TWAP leads price) but with optimistic
fills. The paper trader measures the **realized fill rate and P&L** we'd actually
get, given real trades and our queue position — the number no backtest can give.

## Components
1. **Market-data ingest** (extends recorder_v2)
   - Chainlink WS `crypto_prices_chainlink` → rolling 60s TWAP fair value per asset.
   - Full-depth book: CLOB market WS (`wss://ws-subscriptions-clob.polymarket.com/ws/market`,
     subscribe current window token IDs) with REST `/book` fallback.
   - Live trade feed (CLOB `last_trade_price`/trades, or the on-chain tape) — needed
     to know when a trade would have hit our resting quote.
   - Window discovery: deterministic slug → Gamma → up/down tokens + official winner.
2. **Strategy / decision engine**
   - fair_up = f(60s TWAP vs window-start reference); skew params (K), per-asset toggle.
   - Quote rule: passive bid on the TWAP-favoured side at `fair − spread`, size S,
     max inventory cap, refresh cadence. All params in config.
3. **Paper fill simulator (the honest core)**
   - Place a *virtual* resting order in the *real* book; record our price + the size
     ahead of us at that level (queue position).
   - Fill when real trades cross our level, consuming the queue ahead first
     (queue-aware). Also compute an optimistic variant (fill on any cross) for an
     upper bound. This directly yields the realized fill fraction.
4. **Ledger** (SQLite, isolated; schema mirrors project-magic concepts)
   - `paper_fills` (ts, asset, window, side, price, size, fair, edge, queue_ahead)
   - `paper_positions` (window, net shares, avg cost) → settle at official outcome
   - `paper_pnl` (per-window + running: pnl, capital, ROC, win)
5. **Reporting / Telegram** (reuse project-magic bot pattern; python-telegram-bot)
   - Push on: each simulated fill, each window settlement, periodic P&L summary,
     daily rollup. Access-controlled; chat IDs from env.
   - Env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_PUSH_CHAT_IDS`, `TELEGRAM_ALLOWED_USER_IDS`.
6. **Validation metrics** (the go/no-go for ever considering live)
   - Realized fill rate vs. quoted (the real "f").
   - Realized P&L, win%, ROC — compared to mm_sim v2's prediction.
   - Adverse-selection check: do fills move against us right after?
   - Edge stability across hours/regimes and out-of-sample.
7. **Config** (YAML + env, project-magic style): strategy params, data endpoints,
   telegram, run mode.
8. **Run/ops**: runs locally or on the us-east-1 box (the box matters for realistic
   latency/queue calibration); structured logs + Telegram.

## Hard boundary (mode)
- Single mode: **PAPER**. The process contains **no order-signing and no submission
  code and never loads a private key** — it cannot place a real order by construction.
- Optional **dry-run inspector**: constructs the order *object* (to validate params
  like tick size/min size) and logs it — still never signs or submits.
- **Going live is out of scope here** and is a separate, user-owned step using
  project-magic's existing `execution/live` engine, only after paper validation.

## Explicit non-goals
- No real orders, no fund movement, no key handling, no auto-scaling to live.
- Not wired into project-magic's live executor.

## Success criteria
Runs live for enough sessions that the realized fill rate + P&L are statistically
meaningful, and we can say with evidence whether the strategy is net-positive for a
small account **at real fills** — the honest input to any human decision about capital.
