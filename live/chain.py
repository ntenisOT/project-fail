"""Minimal Polygon chain access for the minter: raw JSON-RPC + eth_account
(already a py_clob_client_v2 dependency) - no web3.py needed.

Safety model: every state-changing call runs eth_estimateGas FIRST - a wrong
encoding or failing condition reverts there, gas-free, before any money moves.
The setup script only ever uses $1-$2 amounts until the round-trip proves out.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

from eth_account import Account
from eth_utils import keccak, to_checksum_address

_send_lock = threading.Lock()   # one EOA = strictly serial nonces (4 assets close together)


class PreflightError(RuntimeError):
    """Raised BEFORE broadcast (estimateGas/nonce/gasPrice stage): no money moved."""


class BroadcastUncertain(RuntimeError):
    """Raised when the tx MAY be in the mempool (timeout/'already known'):
    callers must assume it may mine and reconcile via balances."""

CHAIN_ID = 137
RPCS = ([os.environ["POLYGON_RPC_URL"]] if os.environ.get("POLYGON_RPC_URL") else []) + [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://polygon.drpc.org",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
]

# canonical Polymarket/Polygon contracts
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"          # ConditionalTokens
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"        # bridged (collateral)
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # Circle-native (NOT collateral)
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # CLOB settlement
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"


def _rpc(method, params):
    errs = []
    for url in RPCS:
        try:
            req = urllib.request.Request(url, method="POST",
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0"})
            r = json.load(urllib.request.urlopen(req, timeout=10))
            if "error" in r:
                raise RuntimeError(r["error"])
            return r["result"]
        except Exception as e:
            errs.append(f"{url.split('//')[1].split('/')[0]}: {e}")
    raise RuntimeError(f"all RPCs failed for {method}:\n  " + "\n  ".join(errs))


# ---- minimal ABI encoding ------------------------------------------------
def _w(x) -> str:                                  # one 32-byte word, hex no 0x
    if isinstance(x, int):
        if not 0 <= x < 2 ** 256:
            raise ValueError(f"uint256 out of range: {x}")
        return f"{x:064x}"
    x = x.lower().removeprefix("0x")
    if len(x) > 64 or any(c not in "0123456789abcdef" for c in x):
        raise ValueError(f"bad hex word: {x[:20]}...")
    return x.rjust(64, "0")


def selector(sig: str) -> str:
    return keccak(text=sig)[:4].hex()


def encode_call(sig: str, args: list) -> str:
    """Static args + at most ONE trailing uint256[] dynamic arg."""
    head, tail = [], []
    types = sig[sig.index("(") + 1:-1].split(",") if sig[-2] != "(" else []
    if sum(1 for t in types if t == "uint256[]") > 1 or any(
            t not in ("address", "bytes32", "uint256", "bool", "uint256[]") for t in types):
        raise ValueError(f"encoder supports static types + one uint256[]: {sig}")
    n = len(types)
    for i, (t, a) in enumerate(zip(types, args)):
        if t == "uint256[]":
            head.append(None)                       # patched with offset below
            tail.append(_w(len(a)) + "".join(_w(v) for v in a))
        elif t == "bool":
            head.append(_w(1 if a else 0))
        else:                                       # address/bytes32/uint256
            head.append(_w(a))
    off = 32 * n
    out = []
    for h in head:
        if h is None:
            out.append(_w(off))
        else:
            out.append(h)
    return "0x" + selector(sig) + "".join(out) + "".join(tail)


# ---- reads ---------------------------------------------------------------
def call(to: str, data: str) -> str:
    return _rpc("eth_call", [{"to": to, "data": data}, "latest"])


def erc20_balance(token: str, owner: str) -> float:
    r = call(token, encode_call("balanceOf(address)", [owner]))
    return int(r, 16) / 1e6


def pol_balance(owner: str) -> float:
    return int(_rpc("eth_getBalance", [owner, "latest"]), 16) / 1e18


def ctf_balance(owner: str, position_id: int) -> float:
    r = call(CTF, encode_call("balanceOf(address,uint256)", [owner, position_id]))
    return int(r, 16) / 1e6


def position_ids(condition_id: str) -> tuple[int, int]:
    """ERC1155 ids for outcome slots 1 and 2 (Up/Down) under USDC.e."""
    ids = []
    for index_set in (1, 2):
        coll = call(CTF, encode_call(
            "getCollectionId(bytes32,bytes32,uint256)",
            ["0" * 64, condition_id, index_set]))
        if len(coll) != 66 or int(coll, 16) == 0:   # degraded RPC returning 0x/empty
            raise RuntimeError(f"bad getCollectionId result: {coll!r}")
        pid = call(CTF, encode_call(
            "getPositionId(address,bytes32)", [USDC_E, coll]))
        if len(pid) != 66:
            raise RuntimeError(f"bad getPositionId result: {pid!r}")
        ids.append(int(pid, 16))
    return ids[0], ids[1]


# ---- writes --------------------------------------------------------------
def send(key: str, to: str, data: str, desc: str) -> str:
    """Serialized (module lock) so concurrent asyncio.to_thread callers - the
    4 assets share one 300s close boundary - can never sign duplicate nonces.
    Failure semantics: PreflightError = nothing broadcast, $0 moved;
    BroadcastUncertain = MAY be in the mempool, reconcile via balances."""
    with _send_lock:
        acct = Account.from_key(key)
        tx = {"from": acct.address, "to": to_checksum_address(to),
              "data": data, "value": "0x0"}
        try:
            gas = int(_rpc("eth_estimateGas", [tx]), 16)   # reverts here = $0 lost
            gas_price = max(int(int(_rpc("eth_gasPrice", []), 16) * 1.25),
                            30_000_000_000)                # Polygon 25gwei floor + margin
            nonce = int(_rpc("eth_getTransactionCount", [acct.address, "pending"]), 16)
        except Exception as e:
            raise PreflightError(f"{desc}: preflight failed ({e})") from e
        signed = acct.sign_transaction({
            "chainId": CHAIN_ID, "to": to_checksum_address(to), "value": 0,
            "data": data, "gas": int(gas * 1.3), "gasPrice": gas_price, "nonce": nonce})
        raw = (signed.raw_transaction if hasattr(signed, "raw_transaction")
               else signed.rawTransaction).hex()
        if not raw.startswith("0x"):
            raw = "0x" + raw                   # some eth_account versions omit it
        try:
            txh = _rpc("eth_sendRawTransaction", [raw])
        except Exception as e:
            # 'already known'/'nonce too low' after a timed-out first attempt, or a
            # response timeout: the tx may be live - NEVER report as clean failure.
            raise BroadcastUncertain(f"{desc}: broadcast uncertain ({e})") from e
        for _ in range(60):                                # ~2 min
            time.sleep(2)
            try:
                rec = _rpc("eth_getTransactionReceipt", [txh])
            except Exception:
                continue
            if rec:
                if int(rec["status"], 16) != 1:
                    raise RuntimeError(f"{desc}: tx {txh} REVERTED")
                print(f"  {desc}: OK  tx {txh[:18]}...  gas {int(rec['gasUsed'],16):,}")
                return txh
        raise BroadcastUncertain(f"{desc}: tx {txh} not mined in 2min (may mine later)")


def approve(key, token, spender, amount_usd):
    return send(key, token,
                encode_call("approve(address,uint256)", [spender, int(amount_usd * 1e6)]),
                f"approve {amount_usd:.2f} USDC -> {spender[:10]}")


def split(key, condition_id, amount_usd):
    return send(key, CTF, encode_call(
        "splitPosition(address,bytes32,bytes32,uint256[],uint256)",
        [USDC_E, "0" * 64, condition_id, [1, 2], int(amount_usd * 1e6)]),
        f"splitPosition ${amount_usd:.2f}")


def merge(key, condition_id, amount_usd):
    return send(key, CTF, encode_call(
        "mergePositions(address,bytes32,bytes32,uint256[],uint256)",
        [USDC_E, "0" * 64, condition_id, [1, 2], int(amount_usd * 1e6)]),
        f"mergePositions ${amount_usd:.2f}")
