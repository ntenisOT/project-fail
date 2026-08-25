#!/usr/bin/env python3
"""One bounded post-only CLOB round trip; dry-run unless explicitly armed."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from live.order_probe import cancel_and_verify, choose_probe_order, open_order_ids
from paper import envload
from paper.market_metadata import fetch_active_market

HOST = "https://clob.polymarket.com"
GEO_URL = "https://polymarket.com/api/geoblock"
EXECUTE_ACK = "CYPRUS_ELIGIBLE_MAX_5_USD"


def _geoblock() -> dict[str, object]:
    request = urllib.request.Request(
        GEO_URL, headers={"User-Agent": "project-fail-probe/1"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("unexpected geoblock response")
    return value


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-usd", type=float, default=5.0)
    parser.add_argument("--accept-ie-api-doc-conflict", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    if not 1.0 <= args.max_usd <= 5.0:
        raise SystemExit("--max-usd must be between 1 and 5")
    now = time.time()
    if not 30 <= now % 300 <= 210:
        raise SystemExit("run between T+30 and T+210 of a five-minute window")

    from py_clob_client_v2 import ClobClient  # type: ignore[import-not-found]

    market = fetch_active_market("btc", int(now // 300) * 300)
    if market is None:
        raise SystemExit("active BTC market unavailable")
    public = ClobClient(HOST, 137)
    books = {token: public.get_order_book(token)
             for token in (market.up_token, market.down_token)}
    plan = choose_probe_order(market, books, args.max_usd)
    geo = _geoblock()
    country = str(geo.get("country") or "?")
    print(
        f"plan: {market.slug} {plan.outcome} BUY {plan.size:.1f} @ {plan.price:.2f} "
        f"(${plan.notional:.2f}); current best bid {plan.best_bid:.2f}"
    )
    print(f"source geoblock: blocked={bool(geo.get('blocked'))} country={country}")
    if not args.execute:
        print("DRY RUN: no order was signed, submitted, or cancelled")
        return

    envload.load()
    if os.environ.get("LIVE_LATENCY_ACK") != EXECUTE_ACK:
        raise SystemExit(f"set LIVE_LATENCY_ACK={EXECUTE_ACK} for this one probe")
    if os.environ.get("PHYSICAL_COUNTRY") != "CY":
        raise SystemExit("PHYSICAL_COUNTRY=CY acknowledgement is required")
    blocked = bool(geo.get("blocked"))
    if blocked and not (country == "IE" and args.accept_ie_api_doc_conflict):
        raise SystemExit("source IP is blocked; refusing the order endpoint")
    if blocked:
        print("WARNING: proceeding under the developer-doc Ireland API exception")

    from py_clob_client_v2 import (  # type: ignore[import-not-found]
        OrderArgs, OrderType, PartialCreateOrderOptions,
    )
    from live.executor import Clob

    clob = Clob()
    baseline = open_order_ids(clob.c.get_open_orders())
    if baseline:
        raise SystemExit(f"refusing: account already has {len(baseline)} open orders")
    if clob.collateral_balance() + 1e-9 < plan.notional:
        raise SystemExit("insufficient collateral for bounded probe")

    order_args = OrderArgs(
        token_id=plan.token, price=plan.price, size=plan.size, side="BUY",
    )
    options = PartialCreateOrderOptions(
        tick_size=plan.tick_size, neg_risk=plan.neg_risk,
    )
    clob.c.create_order(order_args, options)  # warm metadata/version only
    signed_at = time.perf_counter()
    signed = clob.c.create_order(order_args, options)
    sign_ms = 1000 * (time.perf_counter() - signed_at)

    order_id: str | None = None
    post_ms = 0.0
    try:
        posted_at = time.perf_counter()
        response = clob.c.post_order(signed, OrderType.GTC, post_only=True)
        post_ms = 1000 * (time.perf_counter() - posted_at)
        if not isinstance(response, dict) or not response.get("success", True):
            raise RuntimeError(f"order rejected: {response!r}")
        order_id = str(response.get("orderID") or response.get("order_id") or "") or None
        if order_id is None:
            raise RuntimeError("accepted response had no order id")
    finally:
        current = open_order_ids(clob.c.get_open_orders())
        targets = ({order_id} if order_id else set()) | (current - baseline)
        cancel_ms = cancel_and_verify(clob.c, {value for value in targets if value})
        if open_order_ids(clob.c.get_open_orders()) - baseline:
            raise RuntimeError("probe cleanup failed; new orders remain")

    try:
        state = clob.c.get_order(order_id) if order_id else {}
    except Exception as exc:  # cleanup is proven; status is diagnostic only
        state = {"status": f"query failed: {type(exc).__name__}"}
    status = state.get("status", "unknown") if isinstance(state, dict) else "unknown"
    matched = state.get("size_matched", "unknown") if isinstance(state, dict) else "unknown"
    print(f"latency ms: sign={sign_ms:.1f} post_ack={post_ms:.1f} cancel_verified={cancel_ms:.1f}")
    print(f"final: status={status} matched={matched} new_open_orders=0")


if __name__ == "__main__":
    main()
