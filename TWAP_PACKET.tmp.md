# Review packet — claimed TWAP-observability edge on Polymarket 5m binaries

Repo: project-fail, current HEAD. Read the code; do not take this on trust.
You are one of two independent reviewers and have not seen the other's work.
We want this BROKEN if it is breakable. It is about to be traded.

## The market

Polymarket 5-minute BTC up/down binaries. Settlement: a Chainlink
**60-second TWAP**. Up wins if the TWAP at T+300 exceeds the TWAP at T+0.
Taker fee = 0.07*p*(1-p). We record the official TWAP at 1Hz into
paper/paper.db `reference_prices` (asset, observed_at, value_e18, window_s).

## The claim

Because settlement is a 60-second AVERAGE, much of the settling number has
already happened before the window closes. So the outcome should be partly
OBSERVABLE - not predicted - late in the window.

tools/twap_observability.py, 72 BTC windows, agreement between the sign of
the partial signal and the settled outcome:

    T+120 (180s left)  72.2%      T+270 ( 30s left)  95.8%
    T+180 (120s left)  77.8%      T+285 ( 15s left)  98.6%
    T+240 ( 60s left)  93.1%      T+290 ( 10s left) 100.0%

## The alleged edge

tools/twap_edge.py simulates: at the checkpoint read the partial signal, buy
the favoured token on Polymarket, hold to settlement, collect $1 or $0, net of
the taker fee. Entry uses only prints where a TAKER BOUGHT the token
(maker_asset_id = token, taker_asset_id = '0'), i.e. a real ask someone paid,
restricted to prints within 15s of the checkpoint.

    checkpoint          n    hit%  avg entry  avg P&L/share   med size
    T+240 (60s left)   62  95.2%      0.898        +0.0489      8.8sh
    T+270 (30s left)   60  96.7%      0.951        +0.0139      6.0sh
    T+285 (15s left)   63 100.0%      0.991        +0.0080      7.2sh

Break-even at a 0.898 entry is a ~90.6% hit rate. Wilson 95% CI on 62 windows
is [86.5%, 98.3%], which straddles break-even, so we call it NOT established
and estimate 150 windows to settle it.

## What we already believe, which may be wrong

* The market is calibrated at the window level (n~1955, z within +/-0.5).
* Binance does NOT lead the TWAP (right only 38-50% on disagreements).
* A cross-venue hedge is dead: BTC moves ~0.12% per 5 minutes, so hedging
  $0.50 of binary payoff needs ~$416 notional (832x the stake) and the
  round-trip Binance fee alone is $0.17-0.33 against $0.50 of upside.
  (tools/hedge_math.py)

## Attack these specifically

Q1. Is the observability result an artifact of how windows are selected?
    A window is only usable if reference_prices contains BOTH a T+0 and a
    T+300 sample within 4s. The feed has gaps. Could gap-free windows be
    systematically calmer/more decided than gapped ones?

Q2. Is the entry price real? We take the last taker-buy print within 15s
    before the checkpoint. Does that print represent size we could have taken,
    or is it the tail of someone else's sweep at a price that no longer
    existed? med size is ~8.8 shares - is that even the right quantity?

Q3. Is the outcome definition circular? We derive `outcome_up` from the same
    reference_prices series we derive the signal from. If the T+300 sample is
    late or interpolated, the signal at T+290 could be reading its own answer.
    Check paper/reference_feed.py and the `nearest(..., tol=4)` helper.

Q4. Selection on |signal|: windows with |signal| < 1bp are skipped. Does that
    drop exactly the hard cases and inflate the hit rate?

Q5. Survivorship in the token lookup: windows whose gamma token lookup fails
    are skipped. Could that correlate with anything?

Q6. Fee and payoff arithmetic: is 0.07*p*(1-p) applied correctly, and is
    P&L = (1 or 0) - price - fee the right accounting for a taker buy held to
    settlement? Any missing cost (gas, redemption, slippage, adverse fill)?

Q7. paper/reference_view.py is NEW and is the causal path that will decide
    real trades. Its whole job is to never let a strategy see a value before
    it was observed. Try to find a way to make it leak a future sample or a
    stale one. tests/test_reference_view.py has 11 tests; assume they are
    insufficient.

Q8. Anything else that would make us lose money here.

Be specific, cite file:line, and rank by how much money the mistake could
cost. If you think the edge is real, say what the strongest remaining risk is.
