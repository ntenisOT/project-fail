"""Bounded Polygon JSON-RPC primitives for authoritative CTF payout evidence."""

from __future__ import annotations

import json
import time
from typing import Mapping, Sequence
import urllib.request

from eth_utils import keccak

from tools.mint_accounting_inputs import EvidenceError, canonical


CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
CHAIN_ID = 137
MAX_BATCH = 50
MAX_LOG_BLOCK_RANGE = 10_000
CONDITION_RESOLUTION_TOPIC = (
    "0x" + keccak(
        text="ConditionResolution(bytes32,address,bytes32,uint256,uint256[])"
    ).hex()
)


def abi_word(value: str | int) -> str:
    if isinstance(value, int):
        if not 0 <= value < 2**256:
            raise EvidenceError("ABI uint256 is out of range")
        return f"{value:064x}"
    raw = value.lower().removeprefix("0x")
    if len(raw) > 64 or any(char not in "0123456789abcdef" for char in raw):
        raise EvidenceError("ABI word is not canonical hex")
    return raw.rjust(64, "0")


def abi_call(signature: str, *args: str | int) -> str:
    return "0x" + keccak(text=signature)[:4].hex() + "".join(abi_word(arg) for arg in args)


def request(rpc_url: str, payload: object, timeout_s: float, attempts: int) -> object:
    body = canonical(payload)
    for attempt in range(attempts):
        try:
            rpc_request = urllib.request.Request(
                rpc_url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json",
                         "User-Agent": "project-fail-ctf-payout/1"},
            )
            with urllib.request.urlopen(rpc_request, timeout=timeout_s) as response:
                return json.load(response)
        except Exception as exc:
            if attempt + 1 == attempts:
                raise EvidenceError("Polygon payout RPC failed within the retry bound") from exc
            time.sleep(min(0.25 * 2**attempt, 2.0))
    raise AssertionError("unreachable")


def rpc(
    rpc_url: str, method: str, params: list[object], timeout_s: float, attempts: int,
) -> object:
    payload = request(
        rpc_url,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout_s,
        attempts,
    )
    if not isinstance(payload, Mapping) or payload.get("error") is not None:
        raise EvidenceError(f"Polygon RPC returned an error for {method}")
    return payload.get("result")


def batch_calls(
    rpc_url: str,
    calls: Mapping[str, str],
    block_reference: str | Mapping[str, object],
    timeout_s: float,
    attempts: int,
) -> dict[str, str]:
    items = list(calls.items())
    output: dict[str, str] = {}
    for offset in range(0, len(items), MAX_BATCH):
        chunk = items[offset:offset + MAX_BATCH]
        request_rows = [
            {"jsonrpc": "2.0", "id": index, "method": "eth_call",
             "params": [{"to": CTF, "data": data}, block_reference]}
            for index, (_, data) in enumerate(chunk, 1)
        ]
        payload = request(rpc_url, request_rows, timeout_s, attempts)
        if not isinstance(payload, list):
            raise EvidenceError("Polygon batch response is not a list")
        by_id = {row.get("id"): row for row in payload if isinstance(row, Mapping)}
        if set(by_id) != set(range(1, len(chunk) + 1)):
            raise EvidenceError("Polygon batch response IDs are incomplete")
        for index, (key, _) in enumerate(chunk, 1):
            row = by_id[index]
            result = row.get("result")
            if row.get("error") is not None or not isinstance(result, str):
                raise EvidenceError(f"Polygon eth_call failed for {key}")
            raw = result.lower().removeprefix("0x")
            if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
                raise EvidenceError(f"Polygon eth_call returned a malformed word for {key}")
            output[key] = "0x" + raw
    return output


def resolution_logs(
    rpc_url: str,
    conditions: Sequence[str],
    from_block: int,
    to_block: int,
    timeout_s: float,
    attempts: int,
) -> dict[str, Mapping[str, object]]:
    if not 0 <= from_block <= to_block:
        raise EvidenceError("invalid ConditionResolution block bounds")
    wanted = set(conditions)
    if len(wanted) != len(conditions) or not wanted:
        raise EvidenceError("ConditionResolution lookup needs unique conditions")
    grouped: dict[str, list[Mapping[str, object]]] = {condition: [] for condition in conditions}
    segment_start = from_block
    while segment_start <= to_block:
        segment_end = min(to_block, segment_start + MAX_LOG_BLOCK_RANGE - 1)
        payload = request(
            rpc_url,
            {
                "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                "params": [{
                    "address": CTF,
                    "fromBlock": hex(segment_start),
                    "toBlock": hex(segment_end),
                    "topics": [CONDITION_RESOLUTION_TOPIC, list(conditions)],
                }],
            },
            timeout_s,
            attempts,
        )
        if (not isinstance(payload, Mapping) or payload.get("error") is not None
                or not isinstance(payload.get("result"), list)):
            raise EvidenceError("Polygon ConditionResolution lookup failed")
        for raw in payload["result"]:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("topics"), list):
                raise EvidenceError("Polygon ConditionResolution result is malformed")
            topics = raw["topics"]
            condition = str(topics[1]).lower() if len(topics) > 1 else ""
            if condition not in grouped:
                raise EvidenceError("Polygon ConditionResolution result escaped the filter")
            grouped[condition].append(raw)
        segment_start = segment_end + 1
    output: dict[str, Mapping[str, object]] = {}
    for condition, logs in grouped.items():
        if len(logs) != 1:
            raise EvidenceError(f"condition {condition} needs exactly one resolution log")
        output[condition] = logs[0]
    return output
