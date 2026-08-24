"""One-time minter proof: wallet health -> $2 approval -> $1 splitPosition ->
verify both outcome tokens on-chain -> $1 mergePositions -> USDC back.

Run it YOURSELF on the box after adding MINTER_PRIVATE_KEY/MINTER_ADDRESS:

    ./.venv/bin/python -m live.minter_setup

Total at risk: $1 plus ~a cent of gas. Every write runs estimateGas first,
so a wrong assumption reverts BEFORE any money moves. Read-only unless the
wallet is funded and the market check passes.
"""
import json
import os
import time
import urllib.request

from paper import envload

envload.load()

from live import chain  # noqa: E402


def gamma_market(prefix="btc-updown-5m"):
    base = int(time.time()) // 300 * 300
    slug = f"{prefix}-{base}"
    req = urllib.request.Request(
        f"https://gamma-api.polymarket.com/events?slug={slug}",
        headers={"User-Agent": "Mozilla/5.0"})
    ev = json.load(urllib.request.urlopen(req, timeout=10))
    m = (ev[0].get("markets") or [{}])[0]
    return slug, m.get("conditionId"), bool(m.get("negRisk"))


def main():
    key = os.environ.get("MINTER_PRIVATE_KEY")
    addr = os.environ.get("MINTER_ADDRESS")
    if not key or not addr:
        print("MINTER_PRIVATE_KEY / MINTER_ADDRESS not in .env - add them first.")
        return

    print(f"minter wallet: {addr}")
    pol = chain.pol_balance(addr)
    usdce = chain.erc20_balance(chain.USDC_E, addr)
    usdcn = chain.erc20_balance(chain.USDC_NATIVE, addr)
    print(f"balances: POL {pol:.4f} | USDC.e {usdce:.2f} | native USDC {usdcn:.2f}")
    if pol < 0.5:
        print("NEED GAS: send ~$10 of POL (Polygon network) to the wallet first.")
        return
    if usdce < 2:
        if usdcn >= 2:
            print("You hold NATIVE USDC but Polymarket needs USDC.e:")
            print("  MetaMask -> Swap -> from USDC (native) to USDC.e -> confirm.")
        else:
            print("NEED FUNDS: send USDC on the Polygon network to the wallet.")
        return

    slug, cond, neg = gamma_market()
    print(f"test market: {slug} | conditionId {cond} | negRisk={neg}")
    if not cond:
        print("no conditionId from gamma - retry next window.")
        return
    if neg:
        print("MARKET IS NEG-RISK: the direct-CTF path does not apply; the")
        print("adapter route needs porting first. STOPPING (nothing spent).")
        return

    up_id, dn_id = chain.position_ids(cond)
    print(f"position ids: up {str(up_id)[:16]}... dn {str(dn_id)[:16]}...")

    print("STEP 1/3: approve $2.00 USDC.e -> ConditionalTokens")
    chain.approve(key, chain.USDC_E, chain.CTF, 2.00)

    print("STEP 2/3: splitPosition $1.00 (mint 1 Up + 1 Dn)")
    chain.split(key, cond, 1.00)
    bu, bd = chain.ctf_balance(addr, up_id), chain.ctf_balance(addr, dn_id)
    print(f"  on-chain outcome balances: up {bu:.2f} | dn {bd:.2f}")
    if bu < 0.99 or bd < 0.99:
        print("  UNEXPECTED - stopping before merge; investigate.")
        return

    print("STEP 3/3: mergePositions $1.00 (burn the set, reclaim USDC)")
    chain.merge(key, cond, 1.00)
    print(f"final USDC.e: {chain.erc20_balance(chain.USDC_E, addr):.2f} "
          f"(gas paid in POL: ~{pol - chain.pol_balance(addr):.4f})")
    print("MINT ROUND-TRIP PASS - the mint path works end to end.")


if __name__ == "__main__":
    main()
