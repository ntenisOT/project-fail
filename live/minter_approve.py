"""One-time approvals so the minter EOA can SELL its minted outcome tokens on
the CLOB: ConditionalTokens setApprovalForAll -> CTFExchange. Run YOURSELF:

    ./.venv/bin/python -m live.minter_approve

Grants only transfer rights on conditional tokens to Polymarket's canonical
exchange contract - standard for every CLOB trader; revocable any time.
"""
import os

from paper import envload

envload.load()

from live import chain  # noqa: E402


def main():
    key = os.environ.get("MINTER_PRIVATE_KEY")
    addr = os.environ.get("MINTER_ADDRESS")
    if not key or not addr:
        print("MINTER_* missing from .env")
        return
    # 1) ERC1155 approval: lets the exchange settle our CLOB sells
    ap = chain.call(chain.CTF, chain.encode_call(
        "isApprovedForAll(address,address)", [addr, chain.CTF_EXCHANGE]))
    if int(ap, 16) == 1:
        print("exchange approval: already granted")
    else:
        print("approving ConditionalTokens -> CTFExchange (one-time)...")
        chain.send(key, chain.CTF,
                   chain.encode_call("setApprovalForAll(address,bool)",
                                     [chain.CTF_EXCHANGE, True]),
                   "setApprovalForAll(CTFExchange)")
    # 2) USDC.e allowance to ConditionalTokens: consumed by every splitPosition
    # (review finding: the $2 setup allowance would have blocked ALL mints)
    allowance = int(chain.call(chain.USDC_E, chain.encode_call(
        "allowance(address,address)", [addr, chain.CTF])), 16) / 1e6
    if allowance >= 10_000:
        print(f"USDC.e allowance: already ${allowance:,.0f}")
    else:
        print("approving $50,000 USDC.e -> ConditionalTokens (cumulative mint budget)...")
        chain.approve(key, chain.USDC_E, chain.CTF, 50_000.0)
    print("DONE - the minter can mint AND sell on the CLOB.")


if __name__ == "__main__":
    main()
