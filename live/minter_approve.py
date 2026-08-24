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
    ap = chain.call(chain.CTF, chain.encode_call(
        "isApprovedForAll(address,address)", [addr, chain.CTF_EXCHANGE]))
    if int(ap, 16) == 1:
        print("already approved - nothing to do")
        return
    print("approving ConditionalTokens -> CTFExchange (one-time)...")
    chain.send(key, chain.CTF,
               chain.encode_call("setApprovalForAll(address,bool)",
                                 [chain.CTF_EXCHANGE, True]),
               "setApprovalForAll(CTFExchange)")
    print("DONE - the minter can now sell on the CLOB.")


if __name__ == "__main__":
    main()
