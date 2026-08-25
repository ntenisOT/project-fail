# Go-live readiness — per-strategy graduation from paper to live

> **Current status (2026-08-25): NO-GO.** This is a legacy checklist, not a
> readiness claim. `README.md` and the current code are authoritative; the
> `lv_` simulator is not execution parity and the mint edge is unproven.

Paper stays the default. A strategy goes live only through the double opt-in
below, one strategy at a time, smallest possible size first.

## Architecture (who does what)

```
paper/run.py  ──►  paper/live_gate.py  ──►  paper/intents.jsonl  ──►  YOUR executor (live/executor.py)
(strategies)       (risk caps, kill,        (desired quotes,          (holds YOUR keys, signs,
 all simulated      double opt-in)           no keys, no orders)       places/cancels, final risk)
```

- Paper and log-only modes hold no keys and submit no orders. `live/executor.py`
  and `live/mintbot.py` can sign and submit in explicit place mode, so they are
  money-path code and remain behind the closed manual launch gate.
- The executor consumes `paper/intents.jsonl`, diffs desired quotes against its
  tracked resting orders, then applies its own risk checks. That tracking is not
  yet sufficient evidence of exchange-state parity; place mode remains NO-GO.

## Verified market facts (2026-08-23, from gamma + CLOB metadata)

- Fees: `crypto_fees_v2`, **taker-only** (`{rate 0.07, exponent 1, takerOnly: true}`)
  → resting (maker) orders pay **0**; makers additionally earn rebates
  (`rebateRate 0.2`) which our models ignore (conservative).
- Taker fee = `shares × 0.07 × p × (1−p)` (docs-confirmed) → 1.75¢/share at
  p=0.5, 0.2¢/share at p≈0.97 (cheap near-close exits, dear mid-range).
- lock_arb lifts both asks = taker both legs → fee ≈ 3.45¢/set at balanced
  prices vs 1-2¢ gross edge: modeled since fill v2.1; balanced sets need
  YES+NO < ~0.965 to profit after fees.
- Limit orders: **minimum 5 shares** to post, tick 0.01. Partial fills of a
  posted order are valid at any size. Market orders: $1 minimum.
- Recent observed auto-redemption was roughly 5-10 minutes after settlement;
  treat this as deployment-specific and remeasure before sizing bankroll.
  A complete YES+NO set merges to $1 instantly (lock_arb capital recycles).

## Fill model v2 (what the paper numbers now assume)

- Execution-shaped twins hold posted quotes for 0.60s; `sq_*` uses 1.0s as a
  tail-stress case. Both simulate stale-quote pick-off, but neither models CLOB
  POST acknowledgement or exchange-state reconciliation.
- Quotes only post with ≥5-share capacity (bid) / ≥5-share inventory (ask).
- Maker fee 0 (matches metadata). f=0.2 queue-share assumption unchanged.
- Known bias: cancels apply lazily at the next print, so v2 is slightly
  **pessimistic** in sparse tape; v1 (archived `paper_fillv1_*.db`) was the
  optimistic ceiling. Live should land between.

## Checklist to enable ONE strategy

- [ ] ≥ 6h of fill-model-v2 A/B data; strategy is top-3 by pnl$ with sane budget$
- [ ] Copy `paper/live.json.example` → `paper/live.json`; set `enabled` to the
      ONE strategy; keep `max_order_usd: 5`, `max_inventory_usd: 50`,
      `daily_loss_stop_usd: 25` for the first session
- [ ] Start runner with `PAPER_LIVE_INTENTS=1` (otherwise nothing is emitted)
- [ ] Run the bundled executor yourself: `python -m live.executor` (log-only first;
      set LIVE_EXECUTOR_MODE=place in .env when ready for real orders)
- [ ] Confirm current CLOB V2 pUSD and outcome-token approvals (not legacy USDC.e/V1 exchange)
- [ ] Dry pass: watch intents for 15 min with executor in log-only mode; check
      quote churn vs CLOB rate limits before letting it place
- [ ] Kill switch drill: `touch paper/KILL` stops intents instantly — verify once
- [ ] First live session: 1 asset (btc), 30–60 min, then compare live fills vs
      paper fills on the SAME windows (fill count, avg $, adverse-selection rate)

## Open items before scaling size

- [ ] Pull CLOB order/cancel rate limits; budget quote churn per asset
- [ ] Live-vs-paper fill calibration report (auto-diff both ledgers per window)
- [ ] Maker rebate accounting (currently ignored = hidden upside)
- [ ] winner_clone build: open+close participation, sell/buy ≈ 1.0 target,
      near-close taker exit at ≥0.97 (fee ~0.2% there)

## Deploy runbook — Ireland box (AWS eu-west-1)

CLOB matching runs in AWS eu-west-2 (London); eu-west-1 is the closest
non-georestricted region (~1-5 ms RTT). Same region we used for project-magic.

```
git clone https://github.com/ntenisOT/project-fail && cd project-fail
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
# copy .env by hand (scp) - NEVER commit it. Fill keys locally on the box.
python -m live.latency          # read-only GET RTT only; do not infer order latency
python -m live.balance          # confirm funds visible
tmux new -d -s paper 'python -m paper.run'            # 13-arm paper + intents
python -m live.executor         # log-only session first, watch [dry] churn
# when ready: LIVE_EXECUTOR_MODE=place in .env, then
tmux new -d -s exec 'python -m live.executor'         # YOU run this = live
# instant stop from anywhere:  touch paper/KILL
```

`live.latency` measures network RTT, not the file executor or order
acknowledgement. Do not derive `PAPER_REQUOTE_S` from `2xRTT+50ms`; use measured
intent-to-order timing for the actual pipeline and retain queue/fill-model
limitations explicitly.
