"""Read-only balance check for the trading wallet. Uses public Polygon RPC and
the WALLET_ADDRESS from .env (never touches keys). Run: python -m live.balance"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

from paper import envload

envload.load()

RPC = "https://polygon-rpc.com"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"   # bridged USDC.e (Polymarket collateral)
USDC_N = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # native USDC


def call(to: str, data: str):
    req = urllib.request.Request(RPC, headers={"Content-Type": "application/json"},
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                         "params": [{"to": to, "data": data}, "latest"]}).encode())
    r = json.load(urllib.request.urlopen(req, timeout=15))
    return int(r.get("result") or "0x0", 16)


def main():
    addr = os.environ.get("WALLET_ADDRESS", "")
    if not addr.startswith("0x") or len(addr) != 42:
        print("Set WALLET_ADDRESS in .env first (0x + 40 hex, your PUBLIC address).")
        sys.exit(1)
    slot = addr[2:].lower().rjust(64, "0")
    usdce = call(USDC_E, "0x70a08231" + slot) / 1e6
    usdcn = call(USDC_N, "0x70a08231" + slot) / 1e6
    pol = None
    try:
        req = urllib.request.Request(RPC, headers={"Content-Type": "application/json"},
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                             "params": [addr, "latest"]}).encode())
        pol = int(json.load(urllib.request.urlopen(req, timeout=15)).get("result") or "0x0", 16) / 1e18
    except Exception:
        pass
    total = usdce + usdcn
    print(f"address        {addr}")
    print(f"USDC.e         ${usdce:,.2f}   (Polymarket collateral)")
    print(f"USDC (native)  ${usdcn:,.2f}")
    if pol is not None:
        print(f"POL (gas)      {pol:.4f}")
    print(f"\nTOTAL USDC     ${total:,.2f}")
    need = 2 * 50 + 20      # 2 live strategies x $50 inventory cap + buffer
    print(f"needed for test: ~${need} (2 strategies x $50 inventory cap + $20 buffer)")
    print("VERDICT:", "ENOUGH to start the test" if total >= need else
          f"SHORT by ${need - total:,.2f} - either top up or lower max_inventory_usd in paper/live.json")


if __name__ == "__main__":
    main()
