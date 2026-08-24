#!/usr/bin/env python3
"""Per-period benchmark: our paper arms vs REAL wallets on the same recent
windows. Pulls the Ireland ledger, resolves tokens (cached), scores real
wallets from local ClickHouse over the last N hours. Read-only.
Run: python winner_bench.py [--hours 1]"""
import argparse, json, os, sqlite3, subprocess, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import clickhouse_connect

CACHE = "backtest_cache/slug_tokens.json"
DB_LOCAL = "backtest_cache/ireland_paper.db"
S = {"max_query_size": 300_000_000, "max_ast_elements": 20_000_000,
     "max_expanded_ast_elements": 20_000_000, "enable_analyzer": 1}
SET_FAMILY = ("lock_arb", "split_sell", "pair_mm")


def resolve(slug):
    try:
        req = urllib.request.Request(f"https://gamma-api.polymarket.com/events?slug={slug}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        ev = json.load(urllib.request.urlopen(req, timeout=10))
        m = (ev[0].get("markets") or [{}])[0]
        ids = m.get("clobTokenIds"); ids = json.loads(ids) if isinstance(ids, str) else ids
        return slug, (ids[:2] if ids and len(ids) >= 2 else None)
    except Exception:
        return slug, None


def main(hours: float):
    os.makedirs("backtest_cache", exist_ok=True)
    subprocess.run(["scp", "-q", "-i", os.path.expanduser("~/.ssh/pm_deploy"),
                    "ubuntu@3.254.130.64:~/project-fail/paper/paper.db", DB_LOCAL], check=True)
    db = sqlite3.connect(DB_LOCAL)
    tmax = db.execute("SELECT max(ts) FROM settlements WHERE n_fills>0").fetchone()[0]
    t_from = tmax - hours * 3600
    wins = {s: int(o) for s, o in db.execute(
        "SELECT DISTINCT slug, outcome_up FROM settlements WHERE n_fills>0 AND ts>=?", (t_from,))}
    cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    missing = [s for s in wins if s not in cache]
    if missing:
        with ThreadPoolExecutor(14) as ex:
            for s, ids in ex.map(resolve, missing):
                if ids:
                    cache[s] = ids
        json.dump(cache, open(CACHE, "w", encoding="utf-8"))
    rows = []
    for slug, out in wins.items():
        ids = cache.get(slug)
        if not ids:
            continue
        base = int(slug.rsplit("-", 1)[-1])
        rows.append([ids[0], float(out), base])
        rows.append([ids[1], float(1 - out), base])
    if not rows:
        print("(no windows in period)")
        return
    t0 = min(r[2] for r in rows) - 30
    t1 = max(r[2] for r in rows) + 330

    c = clickhouse_connect.get_client(host="localhost", port=8123,
        username="copypoly", password="copypoly", database="copypoly")
    c.command("DROP TABLE IF EXISTS pay_b")
    c.command("CREATE TABLE pay_b (token String, payoff Float64, start_ts UInt32) ENGINE=Memory")
    c.insert("pay_b", rows, column_names=["token", "payoff", "start_ts"])
    LEGS = f"""
      SELECT if(maker_asset_id='0', maker, taker) AS wallet,
             multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
             toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
             -toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS cash,
             toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS net_sh
      FROM trade_history
      WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
        AND (maker_asset_id IN (SELECT token FROM pay_b) OR taker_asset_id IN (SELECT token FROM pay_b))
      UNION ALL
      SELECT if(maker_asset_id='0', taker, maker) AS wallet,
             multiIf(maker_asset_id!='0', maker_asset_id, taker_asset_id) AS token,
             toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS usdc,
             toFloat64(if(maker_asset_id='0', maker_amount_filled, taker_amount_filled))/1e6 AS cash,
             -toFloat64(if(maker_asset_id='0', taker_amount_filled, maker_amount_filled))/1e6 AS net_sh
      FROM trade_history
      WHERE block_timestamp>=toDateTime({t0}) AND block_timestamp<toDateTime({t1})
        AND (maker_asset_id IN (SELECT token FROM pay_b) OR taker_asset_id IN (SELECT token FROM pay_b))
    """
    r = c.query(f"""
      SELECT countIf(pnl > 20), round(sumIf(pnl, pnl > 20), 0), round(sum(vol), 0),
             round(max(pnl), 0), round(quantileIf(0.5)(pnl, pnl > 20), 0)
      FROM (SELECT wallet, sum(cash) + sum(net_sh*payoff) AS pnl, sum(usdc) AS vol
            FROM ({LEGS}) L INNER JOIN pay_b p ON L.token = p.token
            GROUP BY wallet HAVING count() >= 10)""", settings=S).result_rows[0]
    c.command("DROP TABLE IF EXISTS pay_b")
    nw, pool, vol, top, med = r

    ours = db.execute("""SELECT strategy, round(sum(pnl),1) FROM settlements
                         WHERE n_fills>0 AND ts>=? GROUP BY strategy ORDER BY sum(pnl) DESC""",
                      (t_from,)).fetchall()
    import datetime
    f = datetime.datetime.fromtimestamp(t_from, datetime.UTC).strftime("%H:%M")
    t = datetime.datetime.fromtimestamp(tmax, datetime.UTC).strftime("%H:%M")
    print(f"BENCH {f}-{t}Z ({len(wins)} windows) - real market vs our paper arms")
    print(f"REAL: {nw} wallets made >$20 | pool ${pool:,.0f} | best ${top:,.0f} | median winner ${med:,.0f} | mkt vol ${vol:,.0f}")
    sf = {s: p for s, p in ours}
    print(f"OURS set-family: lock {sf.get('lock_arb', 0):+.1f} | split {sf.get('split_sell', 0):+.1f} | pair {sf.get('pair_mm', 0):+.1f}")
    best = ours[:3]
    worst = ours[-2:]
    print("OURS best: " + " | ".join(f"{s} {p:+.1f}" for s, p in best)
          + "   worst: " + " | ".join(f"{s} {p:+.1f}" for s, p in worst))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=1.0)
    main(ap.parse_args().hours)
