"""Strict CTF receipt decoding and adapter-operation attribution.

Only ERC-1155 transfers are evidence; amounts are integer CTF base units.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

CTF_ADDRESS = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
ZERO_ADDRESS = "0x" + "0" * 40
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH_TOPIC = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
DEFAULT_EXCHANGES = frozenset({"0xe111180000d2663c0091e4f400237545b87b996b",
                               "0xe2222d279d744050d28e00520010520000310f59"})


class ReceiptDecodeError(ValueError):
    """A recognized CTF transfer log was malformed."""


def _hex(value: object, *, nibbles: int | None = None) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("expected 0x-prefixed hex string")
    body = value[2:].lower()
    if nibbles is not None and len(body) != nibbles:
        raise ValueError(f"expected {nibbles} hex digits")
    if len(body) % 2 or any(char not in "0123456789abcdef" for char in body):
        raise ValueError("invalid hex string")
    return "0x" + body


def _address(value: object) -> str:
    return _hex(value, nibbles=40)


def _topic_address(value: object) -> str:
    topic = _hex(value, nibbles=64)
    if topic[2:26] != "0" * 24:
        raise ValueError("indexed address has nonzero padding")
    return "0x" + topic[-40:]


def _uint(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value and value.isdecimal():
        result = int(value)
    else:
        raise ValueError(f"{field} must be an integer or decimal string")
    if not 0 <= result < 2**256:
        raise ValueError(f"{field} outside uint256 range")
    return result


def _log_index(value: object) -> int:
    if isinstance(value, bool):
        raise ReceiptDecodeError("invalid logIndex")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.startswith("0x"):
        try:
            result = int(value, 16)
        except ValueError as exc:
            raise ReceiptDecodeError("invalid logIndex") from exc
    else:
        raise ReceiptDecodeError("invalid logIndex")
    if result < 0:
        raise ReceiptDecodeError("negative logIndex")
    return result


@dataclass(frozen=True)
class Candidate:
    source_block_number: int
    source_log_index: int
    source_block_timestamp: int
    tx_hash: str
    condition_id: str
    op: str
    adapter: str
    amount: int
    token_ids: tuple[int, int]

    @classmethod
    def from_mapping(cls, row: Mapping[str, object]) -> "Candidate":
        op = row.get("op")
        if not isinstance(op, str) or op not in {"split", "merge"}:
            raise ValueError("op must be split or merge")
        raw_ids = row.get("token_ids")
        if (
            not isinstance(raw_ids, Sequence)
            or isinstance(raw_ids, (str, bytes))
            or len(raw_ids) != 2
        ):
            raise ValueError("token_ids must contain exactly two uint256 values")
        token_ids = (_uint(raw_ids[0], "token_ids[0]"), _uint(raw_ids[1], "token_ids[1]"))
        if token_ids[0] == token_ids[1]:
            raise ValueError("token_ids must be distinct")
        amount = _uint(row.get("amount"), "amount")
        if amount == 0:
            raise ValueError("amount must be positive")
        source_block = _uint(row.get("source_block_number"), "source_block_number")
        source_log = _uint(row.get("source_log_index"), "source_log_index")
        source_timestamp = _uint(row.get("source_block_timestamp"), "source_block_timestamp")
        if (not 0 < source_block < 2**64 or source_log >= 2**16
                or not 0 < source_timestamp < 2**32):
            raise ValueError("source block/log/timestamp outside ClickHouse field ranges")
        return cls(
            source_block_number=source_block,
            source_log_index=source_log,
            source_block_timestamp=source_timestamp,
            tx_hash=_hex(row.get("tx_hash"), nibbles=64),
            condition_id=_hex(row.get("condition_id"), nibbles=64),
            op=op,
            adapter=_address(row.get("adapter")),
            amount=amount,
            token_ids=token_ids,
        )

    def as_json(self) -> dict[str, object]:
        return {
            "source_block_number": self.source_block_number,
            "source_log_index": self.source_log_index,
            "source_block_timestamp": self.source_block_timestamp,
            "tx_hash": self.tx_hash,
            "condition_id": self.condition_id,
            "op": self.op,
            "adapter": self.adapter,
            "amount": str(self.amount),
            "token_ids": [str(value) for value in self.token_ids],
        }


@dataclass(frozen=True)
class Transfer:
    log_index: int
    operator: str
    from_addr: str
    to_addr: str
    token_ids: tuple[int, ...]
    values: tuple[int, ...]


@dataclass(frozen=True)
class TransferGroup:
    log_indices: tuple[int, ...]
    from_addr: str
    to_addr: str
    values: tuple[int, int]


@dataclass(frozen=True)
class Attribution:
    classification: str
    reason: str
    wallet: str | None = None
    counterparty: str | None = None
    anchor_log_indices: tuple[int, ...] = ()
    proof_log_indices: tuple[int, ...] = ()

    def as_json(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "reason": self.reason,
            "wallet": self.wallet,
            "counterparty": self.counterparty,
            "anchor_log_indices": list(self.anchor_log_indices),
            "proof_log_indices": list(self.proof_log_indices),
        }


def _word(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 32 > len(data):
        raise ReceiptDecodeError("ABI word outside transfer data")
    return int.from_bytes(data[offset : offset + 32], "big")


def _array(data: bytes, offset: int) -> tuple[int, ...]:
    if offset % 32 or offset < 64 or offset + 32 > len(data):
        raise ReceiptDecodeError("invalid TransferBatch array offset")
    length = _word(data, offset)
    if length > 64 or offset + 32 * (length + 1) > len(data):
        raise ReceiptDecodeError("invalid TransferBatch array length")
    return tuple(_word(data, offset + 32 * (index + 1)) for index in range(length))


def decode_ctf_transfers(receipt: Mapping[str, object]) -> tuple[Transfer, ...]:
    raw_logs = receipt.get("logs")
    if not isinstance(raw_logs, list):
        raise ReceiptDecodeError("receipt.logs must be a list")
    transfers: list[Transfer] = []
    seen_indices: set[int] = set()
    for raw_log in raw_logs:
        if not isinstance(raw_log, Mapping):
            raise ReceiptDecodeError("receipt log must be an object")
        try:
            address = _address(raw_log.get("address"))
        except ValueError as exc:
            raise ReceiptDecodeError("invalid receipt log address") from exc
        if address != CTF_ADDRESS:
            continue
        topics = raw_log.get("topics")
        if not isinstance(topics, list) or not topics:
            continue
        try:
            topic0 = _hex(topics[0], nibbles=64)
        except ValueError:
            continue
        if topic0 not in {TRANSFER_SINGLE_TOPIC, TRANSFER_BATCH_TOPIC}:
            continue
        if len(topics) != 4:
            raise ReceiptDecodeError("CTF transfer must have four topics")
        try:
            operator, from_addr, to_addr = (_topic_address(value) for value in topics[1:])
            data_hex = _hex(raw_log.get("data"))
        except ValueError as exc:
            raise ReceiptDecodeError("malformed CTF transfer") from exc
        data = bytes.fromhex(data_hex[2:])
        if len(data) % 32:
            raise ReceiptDecodeError("unaligned CTF transfer data")
        token_ids: tuple[int, ...]
        values: tuple[int, ...]
        if topic0 == TRANSFER_SINGLE_TOPIC:
            if len(data) != 64:
                raise ReceiptDecodeError("TransferSingle data must be two words")
            token_ids, values = (_word(data, 0),), (_word(data, 32),)
        else:
            if len(data) < 128:
                raise ReceiptDecodeError("TransferBatch data is too short")
            ids_offset = _word(data, 0)
            values_offset = _word(data, 32)
            token_ids = _array(data, ids_offset)
            values = _array(data, values_offset)
            if len(token_ids) != len(values) or not token_ids:
                raise ReceiptDecodeError("TransferBatch arrays differ or are empty")
            expected_values_offset = 64 + 32 * (len(token_ids) + 1)
            expected_length = expected_values_offset + 32 * (len(values) + 1)
            if ids_offset != 64 or values_offset != expected_values_offset or len(data) != expected_length:
                raise ReceiptDecodeError("TransferBatch data is not canonical ABI")
        index = _log_index(raw_log.get("logIndex"))
        if index in seen_indices:
            raise ReceiptDecodeError("duplicate CTF transfer logIndex")
        seen_indices.add(index)
        transfers.append(Transfer(index, operator, from_addr, to_addr, token_ids, values))
    return tuple(sorted(transfers, key=lambda transfer: transfer.log_index))


def _groups(transfers: tuple[Transfer, ...], candidate: Candidate, tolerance: int) -> list[TransferGroup]:
    wanted = set(candidate.token_ids)
    result: list[TransferGroup] = []
    singles: dict[tuple[str, str], dict[int, list[Transfer]]] = {}
    for transfer in transfers:
        if len(transfer.token_ids) == 2 and set(transfer.token_ids) == wanted:
            ordered = {token_id: value for token_id, value in zip(transfer.token_ids, transfer.values)}
            if len(ordered) == 2:
                values = (ordered[candidate.token_ids[0]], ordered[candidate.token_ids[1]])
                if values[0] == values[1] and abs(values[0] - candidate.amount) <= tolerance:
                    result.append(TransferGroup((transfer.log_index,), transfer.from_addr, transfer.to_addr, values))
        elif len(transfer.token_ids) == 1 and transfer.token_ids[0] in wanted:
            singles.setdefault((transfer.from_addr, transfer.to_addr), {}).setdefault(
                transfer.token_ids[0], []
            ).append(transfer)
    for (from_addr, to_addr), by_token in singles.items():
        if set(by_token) != wanted:
            continue
        for left in by_token[candidate.token_ids[0]]:
            for right in by_token[candidate.token_ids[1]]:
                if left.values[0] == right.values[0] and abs(left.values[0] - candidate.amount) <= tolerance:
                    result.append(
                        TransferGroup(
                            tuple(sorted((left.log_index, right.log_index))),
                            from_addr,
                            to_addr,
                            (left.values[0], right.values[0]),
                        )
                    )
    return result


def classify_receipt(
    candidate: Candidate,
    receipt: Mapping[str, object],
    *,
    amount_tolerance: int = 0,
    exchanges: frozenset[str] = DEFAULT_EXCHANGES,
) -> Attribution:
    if amount_tolerance < 0:
        raise ValueError("amount_tolerance must be non-negative")
    status = receipt.get("status")
    if isinstance(status, bool) or status not in {1, "0x1", "0x01"}:
        return Attribution("unresolved", "receipt_not_successful")
    try:
        receipt_hash = _hex(receipt.get("transactionHash"), nibbles=64)
        receipt_block = _log_index(receipt.get("blockNumber"))
        normalized_exchanges = frozenset(_address(value) for value in exchanges)
        transfers = decode_ctf_transfers(receipt)
    except (ValueError, ReceiptDecodeError):
        return Attribution("unresolved", "malformed_receipt")
    if receipt_hash != candidate.tx_hash:
        return Attribution("unresolved", "receipt_tx_hash_mismatch")
    if receipt_block != candidate.source_block_number:
        return Attribution("unresolved", "receipt_block_number_mismatch")
    groups = _groups(transfers, candidate, amount_tolerance)
    if candidate.op == "split":
        anchors = [group for group in groups if group.from_addr == ZERO_ADDRESS and group.to_addr == candidate.adapter]
        proofs = [
            group
            for group in groups
            if group.from_addr == candidate.adapter
            and group.to_addr not in {ZERO_ADDRESS, candidate.adapter}
        ]
    else:
        anchors = [group for group in groups if group.from_addr == candidate.adapter and group.to_addr == ZERO_ADDRESS]
        proofs = [
            group
            for group in groups
            if group.to_addr == candidate.adapter
            and group.from_addr not in {ZERO_ADDRESS, candidate.adapter}
        ]
    if not anchors:
        return Attribution("unresolved", "missing_adapter_anchor")
    if len(anchors) != 1:
        return Attribution("unresolved", "ambiguous_adapter_anchor")
    anchor = anchors[0]
    if candidate.op == "split":
        ordered_proofs = [proof for proof in proofs if max(anchor.log_indices) < min(proof.log_indices)]
    else:
        ordered_proofs = [proof for proof in proofs if max(proof.log_indices) < min(anchor.log_indices)]
    if not ordered_proofs:
        return Attribution("unresolved", "missing_ordered_counterparty_transfer", anchor_log_indices=anchor.log_indices)
    if len(ordered_proofs) != 1:
        return Attribution("unresolved", "ambiguous_counterparty_transfer", anchor_log_indices=anchor.log_indices)
    proof = ordered_proofs[0]
    counterparty = proof.to_addr if candidate.op == "split" else proof.from_addr
    classification = "clob_atomic" if counterparty in normalized_exchanges else "explicit_wallet"
    return Attribution(
        classification,
        "exact_ordered_ctf_transfer_proof",
        wallet=counterparty if classification == "explicit_wallet" else None,
        counterparty=counterparty,
        anchor_log_indices=anchor.log_indices,
        proof_log_indices=proof.log_indices,
    )
