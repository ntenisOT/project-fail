# Go-live gate

> **Status: NO-GO.** No current strategy or component is approved to place an
> order, approve collateral, split, merge, or otherwise move money.

## Current architecture

```text
public CLOB market feed -> one maker-fill mechanics probe            -> SQLite + capture
public external feeds   -> passive causal recorder                 -> replay covariates
```

The focused paper runner has no keys and emits no executor intents. The legacy
file-driven executor is disconnected. Mintbot place mode, the retired lockbot,
old approval/setup commands, and CTF split/merge fail closed in code.

## Market and execution facts

- Crypto makers pay no trading fee; maker rebates are excluded from paper
  results because our future share of the daily pool is unknown.
- Crypto takers pay `shares * 0.07 * p * (1-p)`, rounded per match to five decimals.
- Limit orders require at least five shares.
- Repeated Ireland CLOB GET RTT is 27-28 ms median / 31-36 ms p90. The legacy one-second
  file poll added 546 ms median / 928 ms p90 and is no longer in the paper path.
  Paper activates actions after 65 ms: approximately twice GET p90 as a conservative
  cancel/replace proxy. Authenticated POST/cancel timing remains unmeasured.
- CLOB V2 uses pUSD and the V2 exchange/collateral adapters. The direct legacy
  USDC.e CTF transaction path is forbidden.

## Strategy evidence required

The current board contains only `basket99`, as a paired-fill mechanics probe;
it is not a promotion candidate. The falsified `mintcycle5` mechanic remains in
tests and frozen archives, not the always-on board. The required sample size is
currently unknown: the
Gen60–72 archives do not provide a clean, immutable pre-period for the exact
endpoint and board. A future candidate cannot advance unless a frozen design
first derives its horizon from prior per-window variance, serial dependence,
an explicit economic hurdle, and a predeclared stopping rule. It must then pass
a non-overlapping future replication cohort. No interim chart or arbitrary
window count can substitute for that design.

A candidate must also demonstrate all of the following:

- One observation per complete market window, with all clips collapsed and
  every invalid/disconnected window's known exposure reported alongside valid
  PnL.
- Positive PnL and return on conservative overlapping bankroll, not merely high
  win rate or volume.
- The result is not explained by one asset, one outcome direction, or a handful
  of windows.
- Fee-inclusive paired buy sum remains below $1.
- The adverse inventory floor, including known exposure in invalid windows, is
  positive; the 50-cent neutral mark is diagnostic only and never a ranking or
  promotion metric.
- A first-leg fill constrains the later opposite-token quote; simultaneous quote
  sums are not accepted as evidence of realized pair economics.
- Unmatched inventory and tail losses remain inside explicit caps.
- Delayed actions revalidate current holdings; negative simulated inventory
  invalidates the generation instead of manufacturing short sales.
- Trade matching uses exchange event time; delayed prints that predate the
  simulated order activation are rejected and counted.
- Any market-feed reconnect invalidates the active window; rolling server-event
  lag must remain bounded rather than hiding a slow consumer behind buffering.
- Queue-ahead consumption, quote residence, post/cancel rate, and fill rate are
  credible rather than print-skimming assumptions.
- Any claimed maker edge survives a predeclared fill-degradation surface and a
  later authorized calibration of queue-fill probability. The 65 ms action
  delay is a sensitivity proxy, not authenticated order latency.
- Mint cannot advance unless new forensics first establish a mint-specific edge
  and the Safe/Relayer V2 path is independently proven. Current evidence rejects
  mint-and-ask as the winner replica and repeated mint churn as profitable.

A directional overlay is also closed. It must show, on non-overlapping
out-of-sample periods, that the lower confidence bound on fair probability clears
the executable price plus taker fee, slippage, causal feed-age markout, and a
safety margin. The public last-trade trend proxy fails this net executable-edge
test before the near-resolution interval.

Early negative evidence may stop a hypothesis only through a predeclared
anytime-valid futility rule or a hard integrity/exposure stop. There is no early
acceptance.

## Execution evidence required

Before any strategy can place an order:

- Add the authenticated CLOB user/order channel and reconcile every order,
  trade, position, collateral balance, and partial fill.
- Stop replenishment whenever fill state is unknown; position polling alone is
  insufficient.
- Measure order POST acknowledgement and verified cancel/replace latency from
  Ireland.
- Prove startup, stale-feed, KILL, window-close, and shutdown cancellation with
  authoritative open-order evidence.
- Port and independently verify the V2 collateral-adapter split/merge route.
- Compare paper queue estimates against a no-money shadow/order-log generation.

## Safe operations

```bash
# paper only
tmux new -d -s paper 'cd ~/project-fail && ./.venv/bin/python -m paper.run'

# feed and quote decisions only; place mode is hard-disabled
tmux new -d -s mintbot 'cd ~/project-fail && MINTBOT_MODE=shadow ./.venv/bin/python -m live.mintbot'

# immediate brake
touch ~/project-fail/paper/KILL
```

Every restart gets a fresh database generation. Stop the exact tmux session,
wait for its child PID to exit, archive the database and intent/log artifacts,
then restart. Never use broad `pkill`, never overwrite `.env`, and never publish
a place-mode command while this gate is closed.
