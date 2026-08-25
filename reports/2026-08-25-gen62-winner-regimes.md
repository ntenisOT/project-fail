# Gen62 results and current winner regimes

## Gen62 run

- Revision: `63ad9ffa03e8d534ebbf5d4a30f7d5cdb7a87461`
- Ireland paper runtime: 2026-08-25 15:29:18-15:51:21 UTC
- Configuration: BTC only, 65 ms simulated action latency, 400 ms freshness
  cutoff, unchanged five-arm Gen61 strategy board, no order placement
- DB SHA-256:
  `cf37fd9e161b8a31aec03518da882df0724f04e15832ab76b50ebd44e33b5b65`
- Log SHA-256:
  `0e3dcc7d8745fd5a1ca9cc99263647b21934ef36fe4968dac4fca420d55dbca0`
- Remote archive: `paper/paper_gen62_shadow_20260825T155121Z.{db,log}`

The archive passed SQLite integrity checking. It contains 116 fills, four
market-level outcomes, 15 valid strategy settlements, five invalid startup
strategy-windows, and 1,248 official reference samples.

## Strategy evidence

| Strategy | Valid windows | PnL | Neutral | Worst | Unmatched |
|---|---:|---:|---:|---:|---:|
| mintcycle5 | 3 | +$0.45 | +$0.45 | +$0.45 | 0.0 |
| mintcycle20 | 3 | -$0.15 | +$2.35 | -$0.15 | 5.0 |
| minthedge60p95 | 3 | -$0.15 | +$2.35 | -$0.15 | 5.0 |
| basket99 | 3 | +$1.80 | -$0.54 | -$5.60 | 10.1 |
| basket99c180 | 3 | +$1.50 | +$1.66 | -$0.90 | 5.1 |

The mint5 arm completed all three cycles at a $1.03 sell sum. The 20-set arms
earned more paired edge, reopened inventory, and lost it to one toxic five-share
residual. The full-depth hedge was due once but completed zero shares: it saw
both insufficient full depth and a best fee-inclusive repair sum of only $0.843.

Basket99's headline profit was direction. Its $0.939 average pair and +$2.04
paired edge were real, but late residue produced negative neutral economics and
a -$5.60 adverse floor. The T+180 cutoff avoided one late open pair and was
materially safer in this cohort.

Across Gen60-62, the identical mintcycle5 arm has eight valid windows, six
complete cycles, 75% completion, +$1.15 neutral mechanics, and -$3.85 realized
and adverse PnL. Three clean Gen62 wins therefore do not establish profitability.

## Signal and latency

Gen62 T+30 official-TWAP signs were +2.36, +0.83, and -5.01 bp; all three
matched the official winner. This remains a tiny, same-regime observation. The
first mint sale was the winner in two of the three windows, demonstrating that
correct direction does not guarantee favorable passive selection.

The market feed had zero reconnects. Normal event age was about 9-10 ms at p50,
local ordered-queue residence stayed at or below 5 ms, and the largest source
tail was 3.266 seconds. One second is not the baseline; intermittent tails are
the operational risk.

The Gen62 outcome ledger correction worked: the partial startup market's winner
was persisted even though all strategy arms were invalid, and the shadow audit
reported the missing opening explicitly rather than silently dropping it.

## Corrected ClickHouse refresh

All queries used official resolved BTC windows, V2-normalized fills, exact token
lifecycle mapping, explicit taker fees, and no maker-rebate assumption.

### Current four hours: 2026-08-25 11:25-15:20 UTC

`0x0cb…` led with +$7,067 on $79,536. It traded both outcomes in 93.8% of 48
markets, but exact FIFO buy-pair coverage was only 58.7%, with $0.934 average
pair, 54 s / 166 s completion d50/d90, 48.8% maker flow, and 45,020 residual
shares. Its T+30 trade-flow call hit 70.5%, but it agreed with the public tape
often and won only 50% of 16 disagreements.

### Current 24 hours: 2026-08-24 15:25 through 08-25 15:20 UTC

The same wallet made +$5,894 on $515,784. At T+30 its directional inventory hit
61.3%; by market end, estimated directional contribution was +$15,858 and
neutral mechanics were -$9,963. It underperformed the public favorite at every
measured cutoff. The public T+60 favorite showed an optimistic +3.89 cents per
share after 1% slippage and modeled taker fees.

### Prior 24 hours: 2026-08-23 15:25 through 08-24 15:20 UTC

`0xb27…` was the clean inventory leader: +$18,844 on $1.28m, 99.5% maker,
96.8% FIFO pair completion, $0.980 average pair, 98.2% maker/maker pairs, and
9 s / 43 s d50/d90. Its neutral mechanics were about +$30,251 while direction
cost approximately $11,106. The public T+60 favorite lost an optimistic 3.83
cents per share in this preceding period.

## Conclusions

1. The original universal mint-and-ask winner story is false. No direct CTF
   split value appeared at these trading addresses.
2. There are at least two regimes: neutral paired-maker accumulation when pair
   prices are sufficiently cheap, and public-momentum trading when the regime
   temporarily rewards it.
3. A fixed T+60 momentum trade is rejected by the time-separated sign flip in
   EV. The official TWAP remains shadow-only.
4. Basket99 needs a late completion policy, not more directional residue. Gen63
   adds a T+270 taker complement only when fees still preserve the rolling
   $0.99 cap.
5. Mint needs bounded repair. Gen63 compares mintcycle5 against a five-set repair
   twin that may consume partial displayed depth above the $0.95 pair floor and
   may round near-minimum residual dust up by no more than 0.1 share.
6. Real-money execution remains NO-GO.
