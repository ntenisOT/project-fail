"""Credential + readiness test. READ-ONLY by construction: derives API creds,
reads collateral balance/allowance and open orders. Never places, cancels, or
signs an order. Never prints secrets. Run: python -m live.credtest"""
from __future__ import annotations

import logging
import os
import sys

from paper import envload

envload.load()
logging.getLogger().setLevel(logging.WARNING)
HOST = "https://clob.polymarket.com"


def main():
    pk = os.environ.get("POLY_PRIVATE_KEY", "")
    funder = os.environ.get("POLY_FUNDER") or None
    sig = int(os.environ.get("POLY_SIGNATURE_TYPE", "2"))
    if not pk or pk.startswith("PASTE_"):
        print("FAIL: POLY_PRIVATE_KEY not set in .env")
        sys.exit(1)
    print(f"config: funder {(funder[:6] + '...' + funder[-4:]) if funder else '(none/EOA)'} | signature_type {sig}")

    from py_clob_client_v2 import ClobClient
    proxy_kw = {"signature_type": sig, "funder": funder} if funder else {}
    try:
        c = ClobClient(HOST, 137, key=pk, **proxy_kw)
        addr = c.get_address()
        print(f"1) key loads, signer address derived: {addr[:6]}...{addr[-4:]}  OK")
    except Exception as e:
        print(f"1) FAIL constructing client: {e.__class__.__name__}: {e}")
        sys.exit(1)
    try:
        creds = c.create_or_derive_api_key()
        c = ClobClient(HOST, 137, key=pk, creds=creds, **proxy_kw)
        print("2) CLOB v2 API credentials derived + accepted  OK")
    except Exception as e:
        print(f"2) FAIL deriving API creds: {e.__class__.__name__}: {e}")
        sys.exit(1)
    try:
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
        bal = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        usdc = float(bal.get("balance", 0)) / 1e6
        allow = bal.get("allowance")
        allow_s = f"{float(allow)/1e6:,.2f}" if allow not in (None, "") else "n/a"
        print(f"3) collateral (USDC) balance ${usdc:,.2f} | exchange allowance {allow_s}  OK")
        need = 120.0
        print(f"   -> {'ENOUGH' if usdc >= need else f'SHORT by ${need-usdc:,.2f}'} for the 2-strategy test (~$120)")
    except Exception as e:
        print(f"3) WARN balance/allowance read: {e.__class__.__name__}: {e}")
    try:
        oo = c.get_open_orders()
        print(f"4) open-orders endpoint (L2 auth): {len(oo or [])} resting orders  OK")
    except Exception as e:
        print(f"4) WARN open orders: {e.__class__.__name__}: {e}")
    print("\ncredtest complete - no orders were created at any point.")


if __name__ == "__main__":
    main()
