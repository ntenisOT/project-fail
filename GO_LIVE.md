# Go-live gate

> **Status: NO-GO.** No current strategy or component is approved to place an
> order, approve collateral, split, merge, or otherwise move money.

## Current architecture

```text
public CLOB market feed -> four-strategy queue-aware paper simulator -> SQLite report
public CLOB market feed -> mintbot shadow quote planner               -> logs only
```

The focused paper runner has no keys and emits no executor intents. The legacy
file-driven executor is disconnected. Mintbot place mode, CTF split/merge, and
the retired lockbot, old approval/setup commands, and CTF split/merge fail
closed in code.

## Market and execution facts

- Crypto makers pay no trading fee; maker rebates are excluded from paper
  results because our future share of the daily pool is unknown.
- Crypto takers pay `shares * 0.07 * p * (1-p)`, rounded per match to five decimals.
- Limit orders require at least five shares.
- Repeated Ireland CLOB GET RTT is 27-28 ms median / 31-33 ms p90. The legacy one-second
  file poll added 546 ms median / 928 ms p90 and is no longer in the paper path.
  Paper activates actions after 65 ms: approximately twice GET p90 as a conservative
  cancel/replace proxy. Authenticated POST/cancel timing remains unmeasured.
- CLOB V2 uses pUSD and the V2 exchange/collateral adapters. The direct legacy
  USDC.e CTF transaction path is forbidden.

## Strategy evidence required

The four current hypotheses are `pair_carry20`, `pair_churn20`,
`pair_inside20`, and `mint_sell20`. A candidate cannot advance unless a clean
generation demonstrates all of the following:

- At least 288 full asset-windows (six hours across four assets); a window with
  first usable paired books more than ten seconds late is excluded.
- Positive PnL and return on conservative overlapping bankroll, not merely high
  win rate or volume.
- The result is not explained by one asset, one outcome direction, or a handful
  of windows.
- Paired buy sum is below $1 and, for churn, paired sell sum is above $1.
- Neutral-mark PnL is positive; realized outcome luck is reported separately.
- A first-leg fill constrains the later opposite-token quote; simultaneous quote
  sums are not accepted as evidence of realized pair economics.
- Unmatched inventory and tail losses remain inside explicit caps.
- Delayed actions revalidate current holdings; negative simulated inventory
  invalidates the generation instead of manufacturing short sales.
- Queue-ahead consumption, quote residence, post/cancel rate, and fill rate are
  credible rather than print-skimming assumptions.
- One-tick price improvement increases completed cycles and neutral PnL versus
  join-best churn without violating the same pair-sum or 65 ms action-delay gates.

Early negative evidence is enough to reject or alter a hypothesis; the minimum
sample is a promotion gate, not a reason to preserve a losing configuration.

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
