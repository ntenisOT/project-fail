"""Fetch and exactly join public Polymarket maker-rebate evidence."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import TYPE_CHECKING, Mapping, Sequence
import urllib.parse
import urllib.request

if TYPE_CHECKING:
    from tools.adapter_receipt_input import InputError, load_input
elif __package__:
    from .adapter_receipt_input import InputError, load_input
else:
    from adapter_receipt_input import InputError, load_input

ENDPOINT = "https://clob.polymarket.com/rebates/current"
OUTPUT_SCHEMA = "project-fail-maker-rebate-evidence-v1"
MAX_RESPONSE_BYTES = 10_000_000
ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
CONDITION_RE = re.compile(r"^0x[0-9a-f]{64}$")
MONEY_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
RESPONSE_DAY_RE = re.compile(r"(?P<day>[0-9]{4}-[0-9]{2}-[0-9]{2})(?:T00:00:00Z)?")


class RebateEvidenceError(ValueError):
    """The source artifact or public response cannot support exact evidence."""


@dataclass(frozen=True)
class MarketArtifact:
    input_sha256: str
    query_sha256: str
    mapping_sha256: str
    markets: dict[str, dict[str, object]]


def _reject_nonfinite(value: str) -> None:
    raise RebateEvidenceError(f"non-finite JSON number is forbidden: {value}")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _wallet(value: object) -> str:
    if not isinstance(value, str) or not ADDRESS_RE.fullmatch(value.lower()):
        raise RebateEvidenceError("wallet must be a 20-byte 0x address")
    return value.lower()


def _day(value: object) -> str:
    if not isinstance(value, str):
        raise RebateEvidenceError("date must be YYYY-MM-DD")
    try:
        parsed = Date.fromisoformat(value)
    except ValueError as exc:
        raise RebateEvidenceError("date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise RebateEvidenceError("date must be canonical YYYY-MM-DD")
    return value


def _response_day(value: object) -> str:
    if not isinstance(value, str):
        raise RebateEvidenceError("response date must be a UTC calendar day")
    match = RESPONSE_DAY_RE.fullmatch(value)
    if match is None:
        raise RebateEvidenceError("response date must be YYYY-MM-DD or YYYY-MM-DDT00:00:00Z")
    return _day(match.group("day"))


def _condition(value: object, field: str = "condition_id") -> str:
    if not isinstance(value, str) or not CONDITION_RE.fullmatch(value.lower()):
        raise RebateEvidenceError(f"{field} must be a 32-byte 0x condition ID")
    return value.lower()


def _money(value: object) -> Decimal:
    if not isinstance(value, str) or not MONEY_RE.fullmatch(value):
        raise RebateEvidenceError("rebated_fees_usdc must be a non-negative decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise RebateEvidenceError("invalid rebated_fees_usdc") from exc
    if not result.is_finite() or result < 0:
        raise RebateEvidenceError("invalid rebated_fees_usdc")
    return result


def _money_text(value: Decimal) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def load_market_artifact(path: Path, max_candidates: int = 10_000) -> MarketArtifact:
    try:
        _, query, input_hash, query_hash = load_input(path, max_candidates)
    except (InputError, OSError) as exc:
        raise RebateEvidenceError(f"invalid candidate artifact: {exc}") from exc
    raw = path.read_bytes()
    if _sha256(raw) != input_hash:
        raise RebateEvidenceError("candidate artifact changed while loading")
    try:
        payload = json.loads(raw, parse_constant=_reject_nonfinite)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RebateEvidenceError("candidate artifact is not valid JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(query, Mapping):
        raise RebateEvidenceError("candidate artifact shape changed while loading")
    mapping = payload.get("market_mapping")
    if not isinstance(mapping, list):
        raise RebateEvidenceError("candidate artifact has no market_mapping")
    markets: dict[str, dict[str, object]] = {}
    for index, row in enumerate(mapping):
        if not isinstance(row, Mapping):
            raise RebateEvidenceError(f"market_mapping row {index} is not an object")
        condition = _condition(row.get("condition_id"), f"market_mapping[{index}].condition_id")
        asset, slug, start = row.get("asset"), row.get("slug"), row.get("start")
        if (not isinstance(asset, str) or not asset or not isinstance(slug, str) or not slug
                or isinstance(start, bool) or not isinstance(start, int) or start < 0):
            raise RebateEvidenceError(f"market_mapping row {index} lacks asset/slug/start")
        if condition in markets:
            raise RebateEvidenceError("duplicate market_mapping condition")
        markets[condition] = {"asset": asset, "slug": slug, "start": start}
    mapping_hash = query.get("market_mapping_sha256")
    if not isinstance(mapping_hash, str):
        raise RebateEvidenceError("candidate artifact lacks market_mapping_sha256")
    return MarketArtifact(input_hash, query_hash, mapping_hash.lower(), markets)


def query_url(day: str, wallet: str) -> str:
    return ENDPOINT + "?" + urllib.parse.urlencode({"date": _day(day), "maker_address": _wallet(wallet)})


def join_response(raw: bytes, day: str, wallet: str, artifact: MarketArtifact) -> dict[str, object]:
    expected_day, expected_wallet = _day(day), _wallet(wallet)
    try:
        payload = json.loads(raw, parse_constant=_reject_nonfinite)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RebateEvidenceError("rebate response is not valid UTF-8 JSON") from exc
    if not isinstance(payload, list):
        raise RebateEvidenceError("rebate response must be an array")
    seen: set[str] = set()
    selected: list[dict[str, object]] = []
    unmapped: list[str] = []
    total = Decimal(0)
    for index, row in enumerate(payload):
        if not isinstance(row, Mapping):
            raise RebateEvidenceError(f"rebate row {index} must be an object")
        condition = _condition(row.get("condition_id"), f"rebate[{index}].condition_id")
        if condition in seen:
            raise RebateEvidenceError(f"duplicate response condition_id: {condition}")
        seen.add(condition)
        if (_response_day(row.get("date")) != expected_day
                or _wallet(row.get("maker_address")) != expected_wallet):
            raise RebateEvidenceError(f"rebate row {index} date or maker mismatch")
        asset_address = _wallet(row.get("asset_address"))
        amount = _money(row.get("rebated_fees_usdc"))
        market = artifact.markets.get(condition)
        if market is None:
            unmapped.append(condition)
            continue
        total += amount
        selected.append({
            "condition_id": condition, "asset_address": asset_address,
            "rebated_fees_usdc": _money_text(amount), **market,
        })
    selected.sort(key=lambda row: str(row["condition_id"]))
    missing = sorted(set(artifact.markets) - seen)
    return {
        "raw_response_sha256": _sha256(raw),
        "counts": {
            "response_rows": len(payload), "selected_rows": len(selected),
            "unmapped_response_conditions": len(unmapped),
            "mapped_conditions_without_response": len(missing),
        },
        "total_rebated_fees_usdc": _money_text(total),
        "selected_rows": selected,
        "unmapped_response_condition_ids": sorted(unmapped),
        "mapped_condition_ids_without_response": missing,
    }


def _fetch(url: str, timeout: float, attempts: int) -> bytes:
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "project-fail-rebate-evidence/1"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RebateEvidenceError("rebate response exceeds byte bound")
            return raw
        except Exception as exc:
            if attempt + 1 == attempts:
                raise RebateEvidenceError(f"rebate fetch failed after {attempts} attempts") from exc
            time.sleep(min(0.25 * (2**attempt), 2.0))
    raise AssertionError("unreachable")


def _revision(repo: Path) -> dict[str, object]:
    paths = [Path(__file__).resolve()] + [repo / "tools" / name for name in (
        "adapter_receipt_core.py", "adapter_receipt_input.py")]
    hashes = {path.name: _sha256(path.read_bytes()) for path in sorted(paths)}
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RebateEvidenceError("unable to resolve Git revision") from exc
    return {"git_head": head, "source_sha256": hashes,
            "revision_sha256": _sha256(_canonical({"git_head": head, "source_sha256": hashes}))}


def evidence_payload(
    raw: bytes, day: str, wallet: str, artifact: MarketArtifact,
    url: str, revision: Mapping[str, object], source_path: Path,
) -> dict[str, object]:
    joined = join_response(raw, day, wallet, artifact)
    return {
        "schema": OUTPUT_SCHEMA, "date": _day(day), "maker_address": _wallet(wallet),
        "query_url": url, "source_artifact": {
            "path": str(source_path), "input_sha256": artifact.input_sha256,
            "query_sha256": artifact.query_sha256,
            "market_mapping_sha256": artifact.mapping_sha256,
            "mapped_conditions": len(artifact.markets),
        },
        "revision": dict(revision),
        "limitations": [
            "conditions absent from the endpoint response are reported, not assumed to be zero",
            "response conditions outside the supplied mapping are hashed and listed but not totaled",
        ],
        **joined,
    }


def _write_exclusive(path: Path, payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return _sha256(encoded)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--market-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=15.0)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args(argv)
    if not 0 < args.timeout_s <= 30 or not 1 <= args.attempts <= 5:
        parser.error("timeout must be in (0,30] and attempts in [1,5]")
    try:
        day, wallet = _day(args.date), _wallet(args.wallet)
        if args.output.exists() or args.output.resolve() == args.market_artifact.resolve():
            raise RebateEvidenceError("output must be new and distinct from the source artifact")
        repo = Path(__file__).resolve().parents[1]
        start_revision = _revision(repo)
        artifact = load_market_artifact(args.market_artifact)
        url = query_url(day, wallet)
        raw = _fetch(url, args.timeout_s, args.attempts)
        payload = evidence_payload(raw, day, wallet, artifact, url, start_revision,
                                   args.market_artifact)
        if _revision(repo) != start_revision:
            raise RebateEvidenceError("source or Git revision changed during fetch")
        digest = _write_exclusive(args.output, payload)
    except (OSError, RebateEvidenceError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps({"output": str(args.output), "output_sha256": digest,
                      "total_rebated_fees_usdc": payload["total_rebated_fees_usdc"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
