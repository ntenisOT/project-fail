# Polymarket 5-min crypto markets — overnight research log

Started 2026-08-22 ~19:15 ET. Goal: figure out whether there's a replicable,
low-capital edge on the 5-minute BTC/ETH/SOL/XRP up/down markets. Read-only /
paper only — nothing here trades or touches a wallet.

## Findings so far (established this session)

1. **Resolution feed = Chainlink 60-second TWAP** (`data.chain.link/streams/*-usd-twap-60s`).
   Resolves Up if end-window TWAP >= start price. NOT spot. This damps last-second
   moves and makes the true signal "running TWAP vs open", which is a data race.

2. **No directional edge (backtest, 3,400 real windows).**
   - 94.9% of windows already decided (<=0.20 or >=0.80) by 30s to close; only ~1.6% still "close".
   - The "last 20-30s when close" idea is negative: -12c to -35c EV, wins <40%.
   - Apparent band-rule winners are sweep noise (t<2, sign-flips). The only stable
     "edge" is buying 0.98 favourites for ~1c = untradeable.

3. **The winners are high-frequency market-makers.** Top-25 profitable wallets on
   these markets: 50k-185k fills / 3 days, 40-100% maker share, quoting across the
   whole window (avg ~150-190s to close), thin margin x huge turnover.

4. **Return on CAPITAL is enormous (and low-capital).** Peak capital deployed is
   tiny; ROC over 3 days: 0xEEbde $31.5k on $3.3k cap (954%), 0x3048 $23k on $931
   (2472%), 0x3BFA (taker) $11k on $301 (3702%). Barrier is bot/speed/adverse-
   selection, NOT capital. (Up-leg-only estimate -> real cap higher, ROC lower, still huge.)

5. **Naive MM does NOT replicate it (mm_sim v1).** Fixed-spread two-sided quoting:
   tight (1-2c) LOSES to adverse selection (-4.5%/win); wide (3-5c) only marginally
   positive (+1-2%/win) even with rewards; wins <46% of windows. Winners' edge must
   be (a) pricing off true TWAP fair value, (b) queue speed/priority, (c) selectivity.

## Deliverables (files)
- `recorder.py` — live order-book recorder (running in background, collecting overnight).
- `backtest.py` — directional-rule backtester on historical trades (cached).
- `winners.py` — per-wallet P&L + style on these markets.
- `capital.py` — return-on-capital estimator.
- `mm_sim.py` — market-making simulator.

## Overnight task queue (each loop iteration does one, appends results below)
- [ ] T1. Fix recorder resolution logging (no resolutions.jsonl yet); verify book capture.
- [ ] T2. Deep-profile 0x3BFA (taker, 3702% ROC) and 0xEEbde (maker): fill price vs
        concurrent mid, entry timing, sizes, hold-to-resolution vs flat, per-window edge.
- [ ] T3. Crack the Chainlink TWAP feed (ws-live-data crypto_prices_chainlink format);
        if not, compute a 60s-TWAP proxy from recorded spot.
- [ ] T4. mm_sim v2: quote around TWAP fair value (not last trade) + inventory skew;
        re-test adverse selection. This is the key test of the winners' real edge.
- [ ] T5. Extend backtest to 2-4 weeks for robustness on the directional verdict.
- [ ] T6. Run mm_sim against the recorder's real overnight L2 book (fills vs actual quotes).
- [ ] T7. Morning summary: can a small account replicate this, and exactly what it needs.

## Iteration log

### 2026-08-23 ~07:00 ET — cohort analysis (cohort.py)
93 profitable wallets on the 5-min markets (3d, pnl>=$2k, >=50 fills). Segments:
- MAKER 37 wallets, $283k pnl, median ROC 594%/3d, cap $1,025, 12k fills, entry 180s
- MIXED 46 wallets, $336k pnl, median ROC 1410%/3d, cap $412, 11k fills, entry 186s
- TAKER 10 wallets, $36k pnl, median ROC 220%/3d, cap $1,320, 1.2k fills, entry 221s
=> 89% (maker+mixed) are the SAME strategy: automated mid-window quoting, tiny
capital, 10k+ fills. Several MIXED wallets are near-identical (22-34k fills, ~3300
markets, 42-43% maker, cap $130-260) => same bot template / fleets, so the true
number of distinct operators is much smaller than 93.
Copyability: makers/mixed NOT copy-tradeable (can't mirror a maker's resting fills;
latency makes you a losing taker). 10 takers are directional but fast; backtest
found no replicable price-signal, so their edge is speed/TWAP => also not copyable.
NOTE: extreme ROCs with cap<=$1 are artifacts of up-leg-only capital; credible
high-ROC wallets have cap $70-$1,000, ROC ~1,500-5,000%/3d.
(Note: overnight auto-loop did NOT fire; this was run on user request in the morning.)

### 2026-08-23 ~morning — winner strategy profiled (profile.py) + Chainlink feed cracked
PROFILE (P&L decomposition, Up leg): the maker/mixed winners make ~0 from round-trip
spread (spread$ deeply NEGATIVE) and ALL profit from SETTLEMENT (settle$ positive).
They post passive bids, ACCUMULATE inventory (keep 76-86%), hold to resolution, end
flat only ~6% of windows. Net-positive settlement => they systematically accumulate
the WINNING side => they must be skewing quotes by fair value. So the strategy is
NOT spread-scalping; it's PASSIVE-LIQUIDITY + HOLD-TO-$1, side-selected by TWAP.
(Taker rows distorted by Up-leg-only view.)

CHAINLINK FEED CRACKED (T3 done): wss://ws-live-data.polymarket.com,
subscribe {"action":"subscribe","subscriptions":[{"topic":"crypto_prices_chainlink",
"type":"update","filters":"{\"symbol\":\"btc/usd\"}"}]}, PING every 5s. Payload
{symbol, value, timestamp, full_accuracy_value}. Symbols btc/usd eth/usd sol/usd xrp/usd.
Also topic "crypto_prices" = Binance spot. => can now record the real settlement
reference and compute the 60s TWAP fair value.

STILL TODO: T4 full-depth L2 recorder (CLOB market channel wss://ws-subscriptions-clob.
polymarket.com/ws/market, subscribe token IDs) + Chainlink TWAP recorder -> feed
mm_sim v2 (passive quotes skewed by TWAP, hold to settlement).
