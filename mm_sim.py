#!/usr/bin/env python3
"""
Market-making simulator for the 5-minute crypto up/down markets.

Tests whether a small two-sided quoting strategy can replicate the winners'
returns, or whether adverse selection eats it. Runs on real historical trade
sequences from ClickHouse (Up token per window).

Model per window:
  - Maintain a fair mid = last trade price. Quote bid=mid-h, ask=mid+h.
  - An incoming trade at price p that crosses a STALE quote fills us for a
    fraction f of its size (queue competition): p<=bid -> we buy at bid;
    p>=ask -> we sell at ask. Buying as price falls / selling as it rises IS
    the adverse-selection cost, captured naturally by the real trade sequence.
  - Settle net inventory at resolution (Up shares pay winner_up).
  - Optional maker rebate credited per filled notional (--reward_bps).

Reports P&L, capital (peak inventory value), and return-on-capital by spread.
Read-only; paper only.
"""
import argparse, json, statistics, sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import clickhouse_connect

CACHE = "backtest_cache/windows.jsonl"
S = {"max_query_size": 300_000_000, "max_ast_elements": 20_000_000,
     "max_expanded_ast_elements": 20_000_000}


def load_windows(step):
    ups = []
    with open(CACHE, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            if "up_token" in o:
                ups.append(o)
    return ups[::step]


def simulate(trades, winner_up, h, f, reward_bps):
    """trades: list of (sc, price, size) ordered by time (sc 300->0)."""
    inv = 0.0      # up shares
    cash = 0.0
    reward = 0.0
    peak_cap = 0.0
    mid = None
    for sc, p, q in trades:
        if p <= 0 or p >= 1:
            continue
        if mid is not None:
            bid, ask = mid - h, mid + h
            fill = f * q
            if p <= bid and bid > 0.005:
                cash -= fill * bid; inv += fill
                reward += fill * bid * reward_bps / 10000.0
            elif p >= ask and ask < 0.995:
                cash += fill * ask; inv -= fill
                reward += fill * ask * reward_bps / 10000.0
        mid = p
        peak_cap = max(peak_cap, abs(inv) * (mid if mid else 0))
    cash += inv * winner_up  # settle
    return cash + reward, peak_cap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=7, help="sample every Nth window")
    ap.add_argument("--reward_bps", type=float, default=0.0)
    ap.add_argument("--fill_frac", type=float, default=0.2)
    args = ap.parse_args()

    wins = load_windows(args.step)
    tokmap = {w["up_token"]: (w["winner_up"], w["start"]) for w in wins}
    toks = list(tokmap.keys())
    t0 = min(s for _, s in tokmap.values()) - 120
    t1 = max(s for _, s in tokmap.values()) + 600
    pay = ",".join(f"('{t}', {s})" for t, (_, s) in tokmap.items())
    print(f"MM sim on {len(toks)} sampled windows | fill_frac={args.fill_frac} reward_bps={args.reward_bps}")

    c = clickhouse_connect.get_client(host="localhost", port=8123,
        username="copypoly", password="copypoly", database="copypoly")
    q = f"""
    WITH pay AS (SELECT token, start_ts FROM values('token String, start_ts UInt32', {pay}))
    SELECT token, sc, price, toks FROM (
      SELECT tr.token AS token, (w.start_ts+300)-tr.ts AS sc, tr.price AS price, tr.toks AS toks
      FROM (
        SELECT multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
               toUInt32(block_timestamp) AS ts,
               toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
               toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS toks,
               if(toks>0, usdc/toks, 0) AS price
        FROM trade_history
        WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
          AND (maker_asset_id IN (SELECT token FROM pay) OR taker_asset_id IN (SELECT token FROM pay))
      ) tr INNER JOIN pay w ON tr.token = w.token
    )
    WHERE sc BETWEEN 0 AND 300 AND toks > 0
    ORDER BY token, sc DESC
    """
    rows = c.query(q, settings=S).result_rows
    seq = {}
    for token, sc, price, toks in rows:
        seq.setdefault(token, []).append((sc, price, toks))
    print(f"windows with trades: {len(seq)}")

    print(f"\n{'half_spread':>11}{'tot_pnl$':>10}{'pnl/win':>9}{'win%':>7}{'avg_cap$':>9}{'ROC/win':>9}")
    for h in [0.01, 0.02, 0.03, 0.05]:
        pnls, caps = [], []
        for tok, (winner, _) in tokmap.items():
            tr = seq.get(tok)
            if not tr:
                continue
            pnl, cap = simulate(tr, winner, h, args.fill_frac, args.reward_bps)
            pnls.append(pnl); caps.append(cap)
        if not pnls:
            continue
        tot = sum(pnls)
        winpct = 100 * sum(1 for x in pnls if x > 0) / len(pnls)
        avgcap = statistics.mean([c for c in caps if c > 0] or [0])
        roc = (statistics.mean(pnls) / avgcap * 100) if avgcap > 0 else 0
        print(f"{h:>11.2f}{tot:>10.2f}{statistics.mean(pnls):>9.3f}{winpct:>6.0f}%{avgcap:>9.2f}{roc:>8.1f}%")
    print("\npnl/win = avg $ per window per 1 quote-unit; ROC/win = pnl per window / avg capital.")
    print("Positive across spreads = MM survives adverse selection on this sample.")


if __name__ == "__main__":
    main()
